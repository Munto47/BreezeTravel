"""Conservative capacity proof for a full, explicitly dated place list.

This does not extract activities. It compares already validated visits with
literal list members, and otherwise leaves the full-output boundary unknown.
"""
from __future__ import annotations

import re
from typing import Literal, Sequence

from app.trip_understanding.models import MAX_TRIP_ACTIVITIES, ProposedMention


CapacityCheck = Literal["BELOW_LIMIT", "EXACT", "OVERFLOW", "UNVERIFIED"]

_DAY_LINE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+)?(?:Day|D)[ \t]*(\d{1,2})(?![\d.．])(?:[ \t]*[:：][ \t]*|[ \t]*$)", re.I)
_UNSAFE = re.compile(
    r"取消|不去|不进|不再|不选|更正|调整|改到|改为|改成|原计划|原安排|"
    r"参考|说明|介绍|例如|比如|引用|选择|方案|备选|可选|二选一|如果|若|或者|或是|"
    r"园内|馆内|内部|包含|附近|途经|经过|路过|[“”\"‘’]"
)
# An unmatched narrative fragment is not an extra place merely because it
# follows a separator. Only the existing narrow place-list noun shape is used.
_PLACE = re.compile(
    r"[A-Za-z0-9\u4e00-\u9fff·\-]{1,39}(?:博物院|博物馆|公园|景区|广场|古镇|步行街|书院|教堂|商圈|酒店|饭店|餐厅|寺|庙|湖|街|桥|馆|园|店|宫|院|塔|楼)"
)


def saturated_source_capacity(source: str, mentions: Sequence[ProposedMention]) -> CapacityCheck:
    """Prove only a same-order, unambiguous pure list; never count model clones.

Physical occurrences, rather than globally unique names, preserve a place
visited once on each of multiple days. Multiple same-name entries within one
day are intentionally not a proof of additional visits.
"""
    if len(mentions) < MAX_TRIP_ACTIVITIES:
        return "BELOW_LIMIT"
    if _UNSAFE.search(source) or any(
        m.role.value != "PLANNED" or not m.atomic_place_name or m.parent_mention_id
        or m.relation_type or m.detail_kind for m in mentions
    ):
        return "UNVERIFIED"
    members: list[tuple[int, str, int, int]] = []
    days: list[int] = []
    day = None
    offset = 0
    prefix = ""
    for line in source.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        heading = _DAY_LINE.match(text)
        if heading:
            day = int(heading[1])
            days.append(day)
            body_offset = heading.end()
        elif day is None:
            prefix += text
            offset += len(line)
            continue
        else:
            body_offset = 0
        body = text[body_offset:]
        if not body.strip():
            offset += len(line)
            continue
        body = body.rstrip(" \t。；;")
        cursor = 0
        for part in re.split(r"[、，,+→]", body):
            name = part.strip()
            start = offset + body_offset + cursor + len(part) - len(part.lstrip())
            if not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff·\-]{1,40}", name):
                return "UNVERIFIED"
            members.append((day, name, start, start + len(name)))
            cursor += len(part) + 1
        offset += len(line)
    if (not days or days != list(range(1, len(days) + 1)) or len(days) > 14
            or len(prefix) > 180 or _PLACE.search(prefix)
            # A caption may state that each day's list is a visit sequence.
            # An action with an object before the first day is outside that
            # list; a bare clause-ending '到访' introduces no extra location.
            or re.search(r"去|到(?!访(?:[，,。；;]|$))|前往|参观|游览|入住|取|午餐|晚餐|早餐", prefix)
            or any(not any(m[0] == day for m in members) for day in days)):
        return "UNVERIFIED"
    if len({(day, name) for day, name, _left, _right in members}) != len(members):
        return "UNVERIFIED"
    expected = set(members)
    bound = [(m.day_index, m.atomic_place_name, m.span_start, m.span_end) for m in mentions]
    bound_set = set(bound)
    if (len(bound_set) != len(bound) or any(item not in expected for item in bound)
            or bound != [item for item in members if item in bound_set]):
        return "UNVERIFIED"
    if len(members) > MAX_TRIP_ACTIVITIES:
        # Existing names already passed source validation. The unmatched
        # sibling must independently have the narrow place-list noun shape;
        # a free-text instruction cannot establish one additional visit.
        return "OVERFLOW" if all(_PLACE.fullmatch(item[1]) for item in members if item not in bound_set) else "UNVERIFIED"
    return "EXACT" if bound == members else "UNVERIFIED"
