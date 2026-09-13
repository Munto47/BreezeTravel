"""Bind inline visit instructions to the validated source occurrence, without IO."""
from __future__ import annotations

from copy import deepcopy
import re
from typing import TYPE_CHECKING

from app.trip_understanding.models import SourceSemanticPlan
from app.trip_understanding.source_visit_supplement import (
    SourceVisitLocation, SourceVisitPurpose, apply_source_visit_supplement,
)

if TYPE_CHECKING:
    from app.trip_understanding.experience_inference import SemanticDraft


def inline_details_schema() -> dict:
    """The model supplies detail meaning, never an independent parent index."""
    variants = []
    for model in (SourceVisitLocation, SourceVisitPurpose):
        shape = deepcopy(model.model_json_schema())
        shape.pop("title", None)
        shape["properties"].pop("parent_index")
        shape["required"].remove("parent_index")
        variants.append(shape)
    return {"anyOf": variants}


def merge_inline_details(original: list, repaired: list) -> list:
    """Keep original order and insert new rows only around shared anchors.

    Rows remain untrusted until the final per-row source validation. An invalid
    new sibling cannot erase a valid old row. No name-only deduplication can
    join different visits or different source evidence.
    """
    rows = deepcopy(original)
    for index, row in enumerate(repaired):
        if row in rows:
            continue
        following = next((rows.index(other) for other in repaired[index + 1:] if other in rows), len(rows))
        preceding = max((rows.index(other) + 1 for other in repaired[:index] if other in rows), default=0)
        rows.insert(max(preceding, following), deepcopy(row))
    return rows


def apply_inline_source_details(source: str, draft: SemanticDraft,
                                proposal: SourceSemanticPlan) -> SourceSemanticPlan:
    from app.trip_understanding.semantic_recovery import _identity
    from app.trip_understanding.semantic_supplement import source_visit_parents

    parents = source_visit_parents(proposal)
    parent_ids = [parent.mention_id for parent in parents]
    rows = []
    for item in draft.activities:
        if not item.source_details:
            continue
        span = _identity(source, item)
        matches = [index for index, parent in enumerate(parents)
            if span == (parent.span_start, parent.span_end)
            and parent.atomic_place_name == item.place_name
            and parent.day_index == item.day_index and parent.role == item.role]
        for raw in item.source_details:
            row = deepcopy(raw) if isinstance(raw, dict) else {}
            # Inline rows have only the wire's location/purpose fields.
            # Do not accept a model-supplied parent or legacy ordinal.
            if len(matches) != 1 or {"parent_index", "occurrence"}.intersection(row):
                row["parent_index"] = 160  # Outside the validator's legal table.
            else:
                row["parent_index"] = matches[0]
            rows.append(row)
    if not rows:
        return proposal
    return apply_source_visit_supplement(source, proposal, rows, parent_ids=parent_ids)


def improves_only_inline_details(source: str, old_draft: SemanticDraft, old: SourceSemanticPlan,
                                 new_draft: SemanticDraft, new: SourceSemanticPlan) -> bool:
    # A still-partial second reply can improve details while preserving every
    # existing visit. Raw list length is not evidence: compare validated rows.
    if old.mentions != new.mentions:
        return False
    before = apply_inline_source_details(source, old_draft, old)
    after = apply_inline_source_details(source, new_draft, new)
    return len(after.mentions) > len(before.mentions)


def source_visit_fragments_covered(source: str, proposal: SourceSemanticPlan) -> bool:
    """Prove narrow instruction fragments were consumed, never infer new items.

    Evidence may quote an entire paragraph while only one child is valid. Only
    actual validated item spans are removed here. Unknown residual nouns or a
    cue with no child for its exact parent keep the generic pending marker.
    This deliberately does not attempt to interpret arbitrary remaining prose.
    """
    from app.trip_understanding.experience_inference import _markdown_visible
    from app.trip_understanding.semantic_supplement import _VISIT_CUE

    roots = [m for m in proposal.mentions if not m.parent_mention_id and m.atomic_place_name]
    details = [m for m in proposal.mentions if m.parent_mention_id and m.relation_type == "INTERNAL_DETAIL"]
    # This is intentionally only a narrow single-visit proof. Multiple visits,
    # branches or days need richer coverage evidence; do not infer their scope
    # or reinterpret an incorrectly independent root merely to clear a warning.
    if not details or len(roots) != 1:
        return False
    scopes = set()
    for cue in _VISIT_CUE.finditer(source):
        # A word inside an already validated atomic name is not an instruction.
        if any(m.span_start <= cue.start() < cue.end() <= m.span_end for m in roots):
            continue
        left = max(source.rfind(mark, 0, cue.start()) for mark in "\n。！？!?；;") + 1
        right = min((p for mark in "\n。！？!?；;" if (p := source.find(mark, cue.end())) >= 0), default=len(source))
        next_root = min((m.span_start for m in roots if m.span_start > cue.start()), default=right)
        previous = max((m for m in roots if m.span_end <= cue.start()), key=lambda m: m.span_end, default=None)
        if not previous or not any(m.parent_mention_id == previous.mention_id
            and m.span_end > cue.start() and m.span_start < min(next_root, right) for m in details):
            return False
        # Punctuation does not end this visit's meaning. An unknown follow-up
        # such as “随后体验云海航船” may have no second explicit internal cue.
        # Removing a global pending marker requires conservative coverage of
        # the remaining source too, including later days and missing parents.
        # Instructions can also precede the first explicit internal cue in
        # this same visit. Start at its actual parent, never at the cue's line.
        scopes.add((min(left, roots[0].span_start), len(source)))
    if not scopes:
        return False
    visible, indices = _markdown_visible(source)
    # An erroneously independent PLANNED row inside an internal list is not
    # proof the internal item was retained. Only actual detail owners qualify;
    # other root names remain unexplained text in this narrow coverage check.
    owners = {m.parent_mention_id for m in details}
    consumed = [(m.span_start, m.span_end) for m in [*[r for r in roots if r.mention_id in owners], *details]]
    glue = re.compile(
        r"园内|馆内|寺内|院内|内部|重点|必看|必逛|必玩|入口|出口|进门|出门|"
        r"按顺序|先后|依次|然后|之后|随后|最后|参观|游览|体验|乘坐|游玩|"
        r"进入|出去|离开|入园|出园|入馆|出馆|入内|先|再|看|去|到|进|出|从|由|经|在|为|是")
    for left, right in scopes:
        if any(d.category == "SOURCE_VISIT_UNRESOLVED" and (d.span_start is None
            or d.span_end is None or left < d.span_end and d.span_start < right) for d in proposal.diagnostics):
            return False
        remaining = "".join(char for char, position in zip(visible, indices, strict=True)
            if left <= position < right and not any(a <= position < b for a, b in consumed))
        remaining = re.sub(r"^\s*(?:#{1,6}\s*)?(?:Day\s*\d{1,2}(?![\d.])|第[一二三四五六七八九十\d]{1,3}天)\s*[：:]?", "", remaining, flags=re.I | re.M)
        remaining = glue.sub("", remaining)
        if re.sub(r"[\s，,、：:；;。.!?！？（）()\[\]【】“”‘’\"'→—\-·]+", "", remaining):
            return False
    return True
