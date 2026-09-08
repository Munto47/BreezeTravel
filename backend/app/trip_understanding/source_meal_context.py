"""Read a bounded, explicitly headed meal item without assigning a visit role."""
from __future__ import annotations

import re


_HEADING = re.compile(
    r"^[ \t]*(?:(?:[-*+]|\d+[.)、])\s+|#{1,6}\s+)?"
    r"(?P<bold>\*\*)?(?P<label>早餐|早饭|午餐|午饭|中午|晚餐|晚饭|晚上|下午茶|夜宵|宵夜)"
    r"(?(bold)\*\*)\s*[：:]"
)


def source_meal_block(source: str, start: int, end: int) -> str | None:
    """Only this item's explicit meal heading can extend a shortened quote.

    A quoted multiline meal stays intact. Unquoted following lines, other list
    items and same-line new time sections are never appended.
    """
    if not 0 <= start < end <= len(source):
        return None
    left = source.rfind("\n", 0, start) + 1
    right = source.find("\n", end)
    if right < 0:
        right = len(source)
    heading = _HEADING.match(source[left:right])
    if heading is None:
        return None
    # A semicolon with a new explicit time section ends this meal item.
    boundary = re.search(r"[；;]\s*(?:早上|上午|中午|下午|傍晚|晚上|早餐|午餐|晚餐)\s*[：:]", source[left:right])
    if boundary:
        right = left + boundary.start()
    if end > right:
        return None
    label_end = left + heading.end('label') + (2 if heading.group('bold') else 0)
    value = (heading.group('label') + source[label_end:right]).strip().rstrip('。')
    return value if len(value) <= 1000 else None
