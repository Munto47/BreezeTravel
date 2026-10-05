"""Literal numbered guide sections can describe an existing overview visit."""
from __future__ import annotations

import re

from app.trip_understanding.models import ActivityRole


_DAY = re.compile(r"(?:第\s*[\d一二两三四五六七八九十]+\s*天|(?:Day|D)\s*\d{1,2})(?![\d.])", re.I)
_HEADING = re.compile(r"(?m)^[ \t]*(?P<prefix>(?:\d+[.)、．]|[一二三四五六七八九十]+、|#{1,6}\s+))"
                      r"[ \t]*(?P<title>[^\n\r]+)")


def _route_line_below_day_heading(source, parent, roots):
    """A labelled route may sit on the line immediately below its day title."""
    from app.trip_understanding.experience_inference import _explicit_day_count

    left = source.rfind('\n', 0, parent.span_start) + 1
    right = source.find('\n', parent.span_end)
    if right < 0:
        return False
    route = source[left:right].strip(' \t*_')
    label = re.match(r'(?:路线|行程路线|游览路线)\s*[：:]\s*', route)
    if label is None:
        return False
    heading_left = source.rfind('\n', 0, max(0, left - 1)) + 1
    heading = source[heading_left:left].strip(' \t\r\n#*_')
    day = _DAY.match(heading)
    if day is None or _explicit_day_count(day[0]) != parent.day_index:
        return False
    listed = [m for m in roots if not m.parent_mention_id and m.day_index == parent.day_index
              and left <= m.span_start < m.span_end <= right and m.atomic_place_name]
    remainder = route[label.end():]
    for mention in sorted(listed, key=lambda m: len(m.atomic_place_name), reverse=True):
        if remainder.count(mention.atomic_place_name) != 1:
            return False
        remainder = remainder.replace(mention.atomic_place_name, '')
    # Only a list of retained names and separators may lend this scope.
    # Narrative visits, unknown members and later return visits stay separate.
    return bool(listed) and re.fullmatch(r'[\s，,、→>\-—*]*', remainder) is not None


def numbered_visit_anchor(source, parent, roots, position):
    """Return a local heading only when it denotes the same unique visit.

    Names in prose, another numbered section and repeated visits cannot lend
    ownership. The caller still checks source evidence, day, branch, actions,
    cancellation and optionality; this only extends a proven section over lines.
    """
    peers = [m for m in roots if m.day_index == parent.day_index
             and m.atomic_place_name == parent.atomic_place_name
             and m.role in {ActivityRole.PLANNED, ActivityRole.OPTIONAL}]
    if len(peers) != 1:
        return None
    headings = list(_HEADING.finditer(source, 0, position))
    if not headings:
        return None
    heading = headings[-1]
    title = heading['title'].strip().strip('*_')
    if title != parent.atomic_place_name or not re.fullmatch(r'\d+[.)、．]', heading['prefix']):
        return None
    left = source.find(title, heading.start(), heading.end())
    right = left + len(title)
    if left < 0 or right > position:
        return None
    if (parent.span_start, parent.span_end) != (left, right):
        # An overview must explicitly introduce its day. A bare prior mention
        # may instead be a previous visit, a quotation or an unrelated reference.
        line_start = source.rfind('\n', 0, parent.span_start) + 1
        prefix = source[line_start:parent.span_start]
        overview = _DAY.match(prefix.lstrip(' \t#*_'))
        if (not overview and not _route_line_below_day_heading(source, parent, roots)) or parent.span_end >= left:
            return None
        same_headings = [h for h in headings if parent.span_end < h.start()
                         and h['title'].strip().strip('*_') == title]
        if len(same_headings) != 1:
            return None
        if _DAY.search(source[parent.span_end:left]):
            return None
    if _DAY.search(source[right:position]):
        return None
    # Markdown or Chinese section headings are included above as boundaries.
    # Unnumbered prose is still subject to the caller's external-action checks.
    return left, right


def narrative_visit_anchor(source, parent, roots, position):
    """Bind a day's list overview to its unique, explicit prose expansion.

    An overview contains only its listed names and connecting words. A real
    visit narrative, repeat, changed day or intervening body stop cannot lend
    ownership to a later detail. No new visit is inferred here.
    """
    peers = [m for m in roots if not m.parent_mention_id and m.day_index == parent.day_index
             and m.role in {ActivityRole.PLANNED, ActivityRole.OPTIONAL}]
    if sum(m.atomic_place_name == parent.atomic_place_name for m in peers) != 1:
        return None
    left = max(source.rfind(mark, 0, parent.span_start) for mark in '\n。；;') + 1
    end = min((p for mark in '\n。；;' if (p := source.find(mark, parent.span_end)) >= 0), default=len(source))
    heading = _DAY.match(source[left:end].lstrip(' \t#*_'))
    if heading is None or end >= position:
        return None
    overview = source[left:end].lstrip(' \t#*_')[heading.end():]
    listed = [m for m in peers if left <= m.span_start < m.span_end <= end]
    if any(not m.atomic_place_name for m in listed):
        return None  # An unnamed activity is not a name-only route overview.
    for mention in sorted(listed, key=lambda m: len(m.atomic_place_name), reverse=True):
        overview = overview.replace(mention.atomic_place_name, '')
    if not re.fullmatch(r'(?:[\s：:，,、→>\-]|和|与|及|留给|安排|路线|行程)*', overview):
        return None
    if _DAY.search(source[end:position]):
        return None
    name = parent.atomic_place_name
    occurrences = list(re.finditer(re.escape(name), source[end + 1:position]))
    if len(occurrences) != 1:
        return None
    start = end + 1 + occurrences[0].start()
    finish = start + len(name)
    clause = max(source.rfind(mark, end, start) for mark in '\n。；;，,') + 1
    if not re.fullmatch(r'\s*(?:先|随后|之后|然后|接着)?(?:在|去|到|参观|游览)?\s*', source[clause:start]):
        return None
    if any((m.atomic_place_name and m.atomic_place_name != name and m.atomic_place_name in source[finish:position])
           or (not m.atomic_place_name and finish <= m.span_start < position) for m in peers):
        return None
    return start, finish


def is_section_reference(source, parent, roots, span):
    """A literal body heading or factual restatement is the retained visit."""
    start, end = span
    if source[start:end] != parent.atomic_place_name:
        return False
    if narrative_visit_anchor(source, parent, roots, end) == span:
        return True
    anchor = numbered_visit_anchor(source, parent, roots, end)
    if not anchor:
        return False
    if anchor == span:
        return True
    line_start = source.rfind('\n', 0, start) + 1
    before = source[line_start:start]
    return bool(re.fullmatch(r'[ \t]*(?:(?:亮点|简介|景点介绍|景区介绍|景点亮点介绍)[：:])?[ \t]*', before)
                and re.match(r'(?:是|位于|坐落于|始建于|为)', source[end:]))
