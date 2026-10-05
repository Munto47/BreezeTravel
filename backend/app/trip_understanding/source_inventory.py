"""Compare an independent source reading with retained semantic occurrences.

This is a model assessment, not proof that the model found every arrangement.
It cannot add or change visits, clear other diagnostics, or authorize edits.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, ValidationError

from app.trip_understanding.models import StrictModel


class InventoryItem(StrictModel):
    quote: str = Field(min_length=1, max_length=100)
    occurrence: int = Field(default=1, ge=1, le=160, strict=True)
    day_index: int | None = Field(default=None, ge=1, le=14, strict=True)
    role: Literal['PLANNED', 'OPTIONAL', 'EXCLUDED', 'REFERENCE', 'PASS_THROUGH']
    kind: Literal['VISIT', 'INTERNAL', 'ENTRY', 'EXIT', 'EXTERIOR_ONLY', 'PICKUP_ONLY'] = 'VISIT'
    parent_quote: str | None = Field(default=None, max_length=100)
    parent_occurrence: int = Field(default=1, ge=1, le=160, strict=True)
    refers_to_occurrence: int | None = Field(default=None, ge=1, le=160, strict=True)


class InventorySegment(StrictModel):
    segment_index: int = Field(ge=0, le=159, strict=True)
    items: list[InventoryItem] = Field(max_length=160)
    unresolved: bool = Field(default=False, strict=True)
    classification: Literal['ARRANGEMENTS', 'CONTEXT', 'REFERENCES'] | None = None
    reference_target_quote: str | None = Field(default=None, max_length=100)
    reference_target_occurrence: int = Field(default=1, ge=1, le=160, strict=True)


class SourceInventory(StrictModel):
    segments: list[InventorySegment] = Field(max_length=160)


def source_segments(source: str) -> list[dict]:
    """Retain exact offsets; numbering is source-only and independent of output."""
    segments = []
    for match in re.finditer(r'[^。！？!?；;\n]+[。！？!?；;\n]*', source):
        if match[0].strip():
            segments.append(dict(index=len(segments), start=match.start(), end=match.end(), text=match[0]))
    return segments


def bind_implicit_references(source, proposal, raw):
    """Bind an empty reference clause only through its validated replacement."""
    from copy import deepcopy
    from app.trip_understanding.conditional_replacement import is_implicit_default_reference
    from app.trip_understanding.source_visit_supplement import _literal_spans

    if raw is None:
        return raw
    inventory = SourceInventory.model_validate(raw)
    segments = source_segments(source)
    corrected = deepcopy(raw)
    for row, wire in zip(inventory.segments, corrected['segments'], strict=True):
        if (row.classification != 'ARRANGEMENTS' or row.items or row.unresolved
                or row.segment_index >= len(segments)):
            continue
        scope = segments[row.segment_index]
        candidates = [m for m in proposal.mentions if not m.parent_mention_id and m.role == 'PLANNED'
            and is_implicit_default_reference(source, proposal, scope, m.raw_text, day_index=m.day_index)]
        if len(candidates) != 1:
            continue
        target = candidates[0]
        spans = _literal_spans(source, target.raw_text)
        span = (target.span_start, target.span_end)
        if span not in spans:
            continue
        wire.update(classification='REFERENCES', reference_target_quote=target.raw_text,
                    reference_target_occurrence=spans.index(span) + 1)
    return corrected


def inventory_covers(source, proposal, raw, *, covered_references: set | None = None) -> bool:
    from app.trip_understanding.experience_inference import SourceAnchorIndex
    from app.trip_understanding.semantic_recovery import explicit_reference_context
    from app.trip_understanding.semantic_supplement import _VISIT_CUE
    from app.trip_understanding.source_visit_supplement import _cancelled_or_conditional
    from app.trip_understanding.source_visit_sections import is_section_reference
    from app.trip_understanding.conditional_replacement import is_replacement_reference, is_implicit_default_reference

    if raw is None or any(d.category == 'SOURCE_VISIT_UNRESOLVED' for d in proposal.diagnostics):
        return False
    try:
        inventory = SourceInventory.model_validate(raw)
    except (ValidationError, ValueError):
        return False
    segments = source_segments(source)
    if not segments or len(segments) > 160 or len(inventory.segments) != len(segments):
        return False
    if sorted(row.segment_index for row in inventory.segments) != list(range(len(segments))):
        return False
    anchors = SourceAnchorIndex(source)
    roots = {m.mention_id: m for m in proposal.mentions if not m.parent_mention_id}
    matched = set()
    direct_matches, reference_targets, reference_spans = set(), set(), set()
    prior_roles = set()
    for row in inventory.segments:
        segment = segments[row.segment_index]
        if row.unresolved:
            return False
        if row.classification == 'REFERENCES':
            if row.items or not row.reference_target_quote:
                return False
            try:
                target_span = anchors.locate(row.reference_target_quote, row.reference_target_occurrence)
            except ValueError:
                return False
            target = next((m for m in roots.values() if (m.span_start, m.span_end) == target_span), None)
            if (target is None or not is_implicit_default_reference(source, proposal, segment,
                    target.raw_text, day_index=target.day_index)):
                return False
            reference_targets.add(target.mention_id)
            reference_spans.add((segment['start'], segment['end']))
            continue
        if row.classification == 'ARRANGEMENTS' and not row.items:
            return False
        if not row.items:
            # Legacy empty replies do not classify a passage. The current wire
            # explicitly distinguishes contextual prose from actual arrangements;
            # every segment and every retained visit still needs its own review.
            if row.classification is None and (_VISIT_CUE.search(segment['text']) or re.search(
                    r'前往|先去|再去|去[\u4e00-\u9fffA-Za-z]|参观|游览|游玩|体验|取消|备选', segment['text'])):
                return False
            continue
        local = SourceAnchorIndex(segment['text'])
        seen = set()
        local_order = {}
        for item in row.items:
            if (item.role in {'REFERENCE', 'PLANNED'} and item.kind == 'VISIT' and not item.parent_quote
                    and item.refers_to_occurrence is None and item.day_index is not None
                    and is_implicit_default_reference(source, proposal, segment, item.quote, day_index=item.day_index)):
                continue
            try:
                from app.trip_understanding.source_visit_supplement import _literal_spans

                # The segment already bounds this occurrence. If its verbatim
                # quote is unique here, a model's global/local count confusion
                # cannot change which source occurrence it identifies. Repeated
                # names within the segment still require the explicit ordinal.
                spans = _literal_spans(segment['text'], item.quote)
                start, end = spans[0] if len(spans) == 1 else local.locate(item.quote, item.occurrence)
                start, end = start + segment['start'], end + segment['start']
                parent_span = anchors.locate(item.parent_quote, item.parent_occurrence) if item.parent_quote else None
            except ValueError:
                return False
            key = (start, end, item.kind, item.role)
            if key in seen:
                return False
            seen.add(key)
            target_span = (start, end)
            if item.refers_to_occurrence is not None:
                if (item.role == 'REFERENCE' and item.kind == 'VISIT' and not item.parent_quote
                        and is_replacement_reference(source, proposal, start, end)):
                    try:
                        referred = anchors.locate(item.quote, item.refers_to_occurrence)
                    except ValueError:
                        return False
                    if not any((m.span_start, m.span_end) == referred and m.role == 'PLANNED'
                               and m.day_index == item.day_index for m in roots.values()):
                        return False
                    reference_spans.add((start, end))
                    continue
                # Only a source-explicit cancellation can restate one retained
                # cancelled visit. Revisit semantics cannot be waived by name.
                if item.role != 'EXCLUDED' or item.kind != 'VISIT' or item.parent_quote:
                    return False
                try:
                    target_span = anchors.locate(item.quote, item.refers_to_occurrence)
                except ValueError:
                    return False
                before = segment['text'][:start - segment['start']]
                if (target_span[1] > start
                    or re.search(r'再去|再访|再次|第二次|第三次|另一次|另外一次', before)
                    or not _cancelled_or_conditional(source, start, end, segment['text'], True)):
                    return False
            candidates = []
            expected_day = item.day_index
            if expected_day is None and item.kind in {'INTERNAL', 'ENTRY', 'EXIT'} and parent_span is not None:
                owners = [m for m in roots.values() if (m.span_start, m.span_end) == parent_span]
                if len(owners) == 1:
                    expected_day = owners[0].day_index
            for mention in proposal.mentions:
                purpose = item.kind in {'EXTERIOR_ONLY', 'PICKUP_ONLY'}
                target = roots.get(mention.parent_mention_id) if purpose else mention
                section_reference = (target is not None and item.kind == 'VISIT' and not target.parent_mention_id
                    and is_section_reference(source, target, list(roots.values()), target_span))
                if target is None or ((target.span_start, target.span_end) != target_span and not section_reference):
                    continue
                if mention.day_index != expected_day:
                    continue
                role = (target.role.value if purpose else
                        'PLANNED' if mention.parent_mention_id and mention.role == 'REFERENCE' else mention.role.value)
                if role != item.role:
                    # The earlier sentence can state the original plan. Only
                    # a separately validated later cancellation may explain
                    # this difference from the retained final role.
                    if (role == 'EXCLUDED' and item.role in {'PLANNED', 'OPTIONAL'}
                        and item.kind == 'VISIT' and item.refers_to_occurrence is None):
                        prior_roles.add(mention.mention_id)
                    else:
                        continue
                if item.kind == 'VISIT':
                    if mention.parent_mention_id or item.parent_quote:
                        continue
                else:
                    kind = 'VISIT' if item.kind == 'INTERNAL' else item.kind
                    if mention.detail_kind != kind or not mention.parent_mention_id:
                        continue
                    parent = roots.get(mention.parent_mention_id)
                    if not purpose and (parent is None or parent_span is None or
                        parent_span != (parent.span_start, parent.span_end)
                        and not is_section_reference(source, parent, list(roots.values()), parent_span)):
                        continue
                candidates.append(mention)
            if len(candidates) == 1:
                mention = candidates[0]
                matched.add(mention.mention_id)
                if item.kind == 'VISIT' and (mention.span_start, mention.span_end) != (start, end):
                    reference_spans.add((start, end))
                if item.refers_to_occurrence is not None:
                    reference_targets.add(mention.mention_id)
                    reference_spans.add((start, end))
                else:
                    direct_matches.add(mention.mention_id)
                if item.kind in {'EXTERIOR_ONLY', 'PICKUP_ONLY'}:
                    # A qualified visit is still that same root occurrence;
                    # the inventory need not duplicate it as a plain VISIT.
                    matched.add(mention.parent_mention_id)
                    mention = roots[mention.parent_mention_id]
                if item.role == 'PLANNED' and mention.role != 'EXCLUDED':
                    group = (mention.day_index, mention.parent_mention_id)
                    previous = local_order.get(group)
                    if previous and previous.mention_id != mention.mention_id and previous.sequence_index >= mention.sequence_index:
                        return False
                    local_order[group] = mention
            elif (not candidates and item.role == 'REFERENCE' and item.kind == 'VISIT'
                  and not item.parent_quote and (explicit_reference_context(source, start, end)
                      or is_replacement_reference(source, proposal, start, end))):
                reference_spans.add((start, end))
                continue
            else:
                return False
    required = {m.mention_id for m in proposal.mentions if m.role in {'PLANNED', 'OPTIONAL', 'EXCLUDED'}
                or m.parent_mention_id}
    complete = (bool(required) and required <= matched and reference_targets <= direct_matches
                and prior_roles <= reference_targets)
    if complete and covered_references is not None:
        covered_references.update(reference_spans)
    return complete
