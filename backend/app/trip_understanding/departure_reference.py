"""Recognize a departure from the immediately preceding retained visit."""
import re


def is_departure_reference(source, proposal, start, end, hints):
    left = max(source.rfind(mark, 0, start) for mark in '\n。！？；;') + 1
    if not re.fullmatch(r'\s*(?:离开|走出)', source[left:start]):
        return False
    if not re.match(r'后(?:再|就|去|到|[，,])', source[end:]):
        return False
    previous = max((m for m in proposal.mentions if not m.parent_mention_id and m.span_end <= left),
                   key=lambda m: m.span_end, default=None)
    if previous is None or previous.role != 'PLANNED' or previous.day_index is None:
        return False
    if re.search(r'Day\s*\d|第[一二三四五六七八九十\d]+天|再次|再访|第二次|第三次',
                 source[previous.span_end:start], re.I):
        return False
    target = next((hint for hint in hints if hint['span_start'] == start and hint['span_end'] == end), None)
    return bool(target and target.get('entity_id') and any(
        hint.get('entity_id') == target['entity_id'] and previous.span_start <= hint['span_start']
        and hint['span_end'] <= previous.span_end for hint in hints))
