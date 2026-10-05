"""Apply explicit second-reading corrections to provisional internal visits."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, ValidationError

from app.trip_understanding.models import SemanticDiagnostic, StrictModel


def _inventory_retains(source, raw, mention):
    from app.trip_understanding.source_inventory import SourceInventory, source_segments
    from app.trip_understanding.source_visit_supplement import _literal_spans

    try:
        inventory = SourceInventory.model_validate(raw)
    except (ValidationError, ValueError):
        return False
    segments = source_segments(source)
    for row in inventory.segments:
        if row.segment_index >= len(segments):
            continue
        segment = segments[row.segment_index]
        for item in row.items:
            if item.kind != 'INTERNAL' or item.day_index != mention.day_index:
                continue
            spans = _literal_spans(segment['text'], item.quote)
            if any((segment['start'] + left, segment['start'] + right) ==
                   (mention.span_start, mention.span_end) for left, right in spans):
                return True
    return False


class DetailReview(StrictModel):
    detail_index: int = Field(ge=0, le=159, strict=True)
    classification: Literal['NAMED_VISIT', 'GENERAL_DESCRIPTION']
    evidence: str = Field(min_length=1, max_length=500)


def reviewable_details(proposal) -> list[dict]:
    return [dict(index=index, mention_id=m.mention_id, name=m.raw_text,
                 parent_id=m.parent_mention_id, start=m.span_start, end=m.span_end)
            for index, m in enumerate(m for m in proposal.mentions
                if m.parent_mention_id and m.detail_kind == 'VISIT'
                and m.relation_type == 'INTERNAL_DETAIL')]


def apply_detail_reviews(source, proposal):
    """Omission in a second answer cannot delete anything.

    Only an explicit classification of an already supplied internal VISIT can
    correct it. Roots, gates, purposes, unknown indices, conflicting reviews and
    evidence from another occurrence are never changed. Source inventory still
    checks the complete resulting plan independently.
    """
    supplied = proposal.binding.get('_reviewable_details', [])
    raw_reviews = proposal.binding.get('_detail_reviews', [])
    reviews = {}
    for raw in raw_reviews:
        try:
            row = DetailReview.model_validate(raw)
        except (ValidationError, ValueError):
            continue
        reviews.setdefault(row.detail_index, []).append(row)
    removed = set()
    conflicts = []
    by_id = {m.mention_id: m for m in proposal.mentions}
    for index, rows in reviews.items():
        if len(rows) != 1 or rows[0].classification != 'GENERAL_DESCRIPTION' or index >= len(supplied):
            continue
        entry, row = supplied[index], rows[0]
        mention = by_id.get(entry['mention_id'])
        if (mention is None or not mention.parent_mention_id or mention.detail_kind != 'VISIT'
                or mention.relation_type != 'INTERNAL_DETAIL'
                or (mention.span_start, mention.span_end) != (entry['start'], entry['end'])):
            continue
        from app.trip_understanding.source_visit_supplement import _literal_spans

        spans = _literal_spans(source, row.evidence)
        if len(spans) != 1 or not (spans[0][0] <= mention.span_start < mention.span_end <= spans[0][1]):
            continue
        if _inventory_retains(source, proposal.binding.get('_source_inventory'), mention):
            conflicts.append(SemanticDiagnostic(category='SOURCE_VISIT_UNRESOLVED',
                field=f'detail_reviews[{index}]', span_start=mention.span_start, span_end=mention.span_end))
            continue
        removed.add(mention.mention_id)
    diagnostics = list(proposal.diagnostics)
    for issue in conflicts:
        if issue not in diagnostics:
            diagnostics.append(issue)
    if not removed and diagnostics == proposal.diagnostics:
        return proposal
    return proposal.model_copy(update={'mentions': [m for m in proposal.mentions if m.mention_id not in removed],
        'diagnostics': diagnostics,
        'unprocessed_count': proposal.unprocessed_count + len(diagnostics) - len(proposal.diagnostics)})
