"""Validate second-answer visit instructions without calls or mainline edits."""
from __future__ import annotations

from collections.abc import Sequence
import re
from typing import Literal

from pydantic import Field, ValidationError

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.failures import INPUT_CAPACITY_EXCEEDED
from app.trip_understanding.models import (
    MAX_TRIP_ACTIVITIES, ActivityRole, ProposedMention, SemanticDiagnostic,
    SourceSemanticPlan, StrictModel,
)


class SourceVisitSupplement(StrictModel):
    parent_index: int = Field(ge=0, le=159, strict=True)
    kind: Literal["VISIT", "ENTRY", "EXIT", "EXTERIOR_ONLY", "PICKUP_ONLY"]
    source_quote: str = Field(min_length=1, max_length=100)
    occurrence: int = Field(default=1, ge=1, le=160, strict=True)
    optional: bool = Field(default=False, strict=True)
    evidence: str = Field(min_length=1, max_length=500)


class SourceVisitPurpose(StrictModel):
    """A purpose belongs to one supplied visit, without a second name index."""
    parent_index: int = Field(ge=0, le=159, strict=True)
    kind: Literal["EXTERIOR_ONLY", "PICKUP_ONLY"]
    optional: bool = Field(strict=True)
    evidence: str = Field(min_length=1, max_length=500)


def _literal_matches(source: str, quote: str) -> list[tuple[tuple[int, int], list[int]]]:
    from app.trip_understanding.experience_inference import _markdown_visible

    # Only paired presentation delimiters may differ. Retain the full-source
    # character map, including when a quote begins inside a bold parent name.
    visible, indices = _markdown_visible(source)
    quoted, _ = _markdown_visible(quote)
    matches = []
    offset = 0
    while quoted and (left := visible.find(quoted, offset)) >= 0:
        matched = indices[left:left + len(quoted)]
        matches.append(((matched[0], matched[-1] + 1), matched))
        offset = left + len(quoted)
    return matches


def _literal_spans(source: str, quote: str) -> list[tuple[int, int]]:
    return [span for span, _ in _literal_matches(source, quote)]


def _visible_slice(source: str, start: int, end: int) -> str:
    from app.trip_understanding.experience_inference import _markdown_visible

    visible, indices = _markdown_visible(source)
    return "".join(char for char, index in zip(visible, indices, strict=True) if start <= index < end)


def _cancelled_or_conditional(source: str, start: int, end: int, evidence: str, optional: bool) -> bool:
    left = max(source.rfind(mark, 0, start) for mark in "\n。；;，,") + 1
    right = min((p for mark in "\n。；;，," if (p := source.find(mark, end)) >= 0), default=len(source))
    before = _visible_slice(source, left, start)
    after = _visible_slice(source, end, right)
    cancelled = re.search(r"(?:取消|不去|不看|不参观|不进入|不进|不再去|跳过)[^。；;，,\n]{0,10}$", before)
    if cancelled and not re.search(r"(?:没有|并未|未|不)\s*$", before[:cancelled.start()]):
        return True
    if re.match(r"\s*(?:已取消|取消|本次不去|不去了|不参观)", after):
        return True
    # A proposed condition cannot disappear while its literal evidence stays.
    return bool(not optional and re.search(r"若|如果|假如|时间(?:充裕|足够|允许)|有(?:时间|余力)|有兴趣",
        _visible_slice(evidence, 0, len(evidence))))


def _shared_purpose_roots(source: str, parent: ProposedMention, roots: list[ProposedMention]):
    """A shared clause can constrain both adjacent named venues, not just last."""
    paired = set()
    last_end = parent.span_end
    for other in sorted(roots, key=lambda item: item.span_start):
        if other.span_start < last_end or other.day_index != parent.day_index:
            continue
        gap = source[last_end:other.span_start]
        if not re.fullmatch(r"[ \t*_]*(?:(?:、|和|及|与|\+|/)[ \t*_]*)+", gap):
            break
        paired.add(other.mention_id)
        last_end = other.span_end
    return [item for item in roots if item.mention_id not in paired]


def _scope_parent(source, anchors, row, parent, roots, start, end, *, explicit_kind=False):
    from app.trip_understanding.experience_inference import SemanticActivity, _validated_internal_parent

    quote = row.source_quote if isinstance(row, SourceVisitSupplement) else source[start:end]
    item = SemanticActivity(source_quote=quote,
        place_name=quote if len(quote) <= 40 else None,
        role=ActivityRole.OPTIONAL if row.optional else ActivityRole.REFERENCE,
        day_index=parent.day_index, parent_source_quote=parent.atomic_place_name, role_evidence=row.evidence)
    return _validated_internal_parent(source, anchors, item, start, end, parent.day_index,
        roots, parent.mention_id if explicit_kind else None) == parent.mention_id


def _gate_anchor(row, quote_indices):
    from app.trip_understanding.experience_inference import _markdown_visible

    name, _ = _markdown_visible(row.source_quote)
    # A short typed quote may include its adjacent direction verb. Its source
    # span/occurrence still covers the original whole quote, not another 门.
    action = re.fullmatch(r"(?P<name>[\w·]{1,38}门)(?P<verb>进入|入园|入馆|入内|出去|离开|进|出)", name)
    if action:
        expected = {"进入", "入园", "入馆", "入内", "进"} if row.kind == "ENTRY" else {"出去", "离开", "出"}
        if (action["verb"] not in expected or
            re.match(r"(?:不(?:从|由|经)?|不要|不再|并非|去|到|(?:从|由|经)不)", action["name"])):
            return None
        # A leading 从/由/经 may be grammar or part of the literal gate name
        # (从化门). Keep the whole instruction for display; never guess a
        # stripped name. Direction still uses the original verb coordinate.
        display = name if name.startswith(("从", "由", "经")) else action["name"]
        return display, quote_indices[action.start("verb") - 1] + 1
    return name, quote_indices[-1] + 1


def _gate_direction(row, source, start, end, evidence_span):
    left, right = evidence_span
    before = _visible_slice(source, left, start).rstrip()
    after = _visible_slice(source, end, right).lstrip()
    # A short literal quote must not cut a preceding negation out of the source.
    clause_start = max(source.rfind(mark, 0, start) for mark in "\n。；;，,") + 1
    source_before = _visible_slice(source, clause_start, start).rstrip()
    if re.search(r"(?:不|不要|不再|并非)(?:从|由|经)?\s*$", source_before):
        return False
    if row.kind == "ENTRY":
        return bool(re.match(r"(?:进入|进|入园|入馆|入内)", after)
            or re.search(r"(?:入口|进门)(?:在|为|是|：|:)\s*$", before))
    return bool(re.match(r"(?:出去|出|离开)", after)
        or re.search(r"(?:出口|出门)(?:在|为|是|：|:)\s*$", before))


def _purpose_supported(row):
    from app.trip_understanding.experience_inference import _markdown_visible

    text, _ = _markdown_visible(row.evidence)
    if re.search(r"俯瞰|眺望|远眺|遥望|望向|远望", text):
        return None  # Do not transfer a viewing object's restriction to its viewpoint.
    if row.kind == "EXTERIOR_ONLY":
        explicit = re.search(r"(?:只|仅)(?:看|拍摄|参观)?(?:外观|外部|外立面)", text)
        if explicit:
            if re.search(r"不|并非|不是", text[max(0, explicit.start() - 3):explicit.start()]):
                return None
            return explicit.span()
        restriction = re.search(r"(?:不|不用|无需|不必)[^。；;\n]{0,8}(?:进|入)(?:馆|园|内|内部|场馆)", text)
        return restriction.span() if restriction and re.search(r"外观|外面|门外|外部|外立面|外广场", text) else None
    action = re.search(r"取[^。；;\n]{0,8}(?:行李|寄存|物品|物)", text)
    return action.span() if (action and re.search(r"(?:只|仅)[^。；;\n]{0,12}取", text)
        and re.search(r"不(?:进|入|参观)[^。；;\n]{0,6}(?:展厅|内部|馆|园)", text)) else None


def _unique_purpose_action(source: str, row: SourceVisitPurpose, evidence_match):
    """Locate one supported action; shortened evidence cannot hide negation."""
    from app.trip_understanding.experience_inference import _markdown_visible

    span, indices = evidence_match
    action = _purpose_supported(row)
    if action is None:
        return None
    visible, _ = _markdown_visible(row.evidence)
    if _purpose_supported(row.model_copy(update={"evidence": visible[action[1]:]})) is not None:
        return None  # Two purpose actions inside one quote remain ambiguous.
    located = (indices[action[0]], indices[action[1] - 1] + 1)
    left = max(source.rfind(mark, 0, located[0]) for mark in "\n。；;") + 1
    right = min((p for mark in "\n。；;" if (p := source.find(mark, located[1])) >= 0), default=len(source))
    context = source[left:right]
    actual = _purpose_supported(row.model_copy(update={"evidence": context}))
    _, context_indices = _markdown_visible(context)
    if actual is None or (left + context_indices[actual[0]], left + context_indices[actual[1] - 1] + 1) != located:
        return None
    if row.optional and not _cancelled_or_conditional(source, *located, context, False):
        return None  # Explicit purpose cannot acquire an unsupported condition.
    if _cancelled_or_conditional(source, *located, context, row.optional):
        return None
    return located


def apply_source_visit_supplement(
    source: str, proposal: SourceSemanticPlan,
    rows: Sequence[SourceVisitSupplement | SourceVisitPurpose | dict], *, parent_ids: Sequence[str],
) -> SourceSemanticPlan:
    """Attach validated children; parent IDs are the request's immutable table.

    This synchronous function neither infers task state from binding nor writes
    SUCCESS. The caller retains normal cancellation/deadline ownership. A bad
    row remains unfinished while valid siblings survive. Capacity is never
    truncated; the existing capacity failure is propagated to the caller.
    """
    from app.trip_understanding.experience_inference import SourceAnchorIndex, _coverage_day_scopes

    if not isinstance(proposal, SourceSemanticPlan):
        raise TypeError("source visit supplement requires a source semantic plan")
    anchors = SourceAnchorIndex(source)
    by_id = {mention.mention_id: mention for mention in proposal.mentions}
    roots = [mention for mention in proposal.mentions if not mention.parent_mention_id]
    additions = []
    reconciled_ids = set()
    addition_origins = {}
    response_visits = {}
    issues = []
    issue_days = {}
    day_scopes = _coverage_day_scopes(source)
    existing = {(m.parent_mention_id, m.detail_kind or "VISIT", m.span_start, m.span_end): m
        for m in proposal.mentions if m.parent_mention_id and m.relation_type == "INTERNAL_DETAIL"}
    purpose_evidence = {(m.parent_mention_id, m.detail_kind, m.role_evidence_start, m.role_evidence_end)
        for m in proposal.mentions if m.detail_kind in {"EXTERIOR_ONLY", "PICKUP_ONLY"}}
    known_issues = {(d.category, d.field, d.span_start, d.span_end) for d in proposal.diagnostics}

    def reject(index, span):
        issue = SemanticDiagnostic(category="SOURCE_VISIT_UNRESOLVED", field=f"source_visits[{index}]",
            span_start=span[0] if span else None, span_end=span[1] if span else None)
        key = (issue.category, issue.field, issue.span_start, issue.span_end)
        if key not in known_issues:
            known_issues.add(key)
            issues.append(issue)
            if span:
                day = next((day for day, left, right in day_scopes if left <= span[0] < span[1] <= right), None)
                if day:
                    issue_days[day] = issue_days.get(day, 0) + 1

    for index, raw in enumerate(rows):
        try:
            if isinstance(raw, (SourceVisitSupplement, SourceVisitPurpose)):
                row = raw
            elif (isinstance(raw, dict) and raw.get("kind") in {"EXTERIOR_ONLY", "PICKUP_ONLY"}
                  and not {"source_quote", "occurrence"}.intersection(raw)):
                row = SourceVisitPurpose.model_validate(raw)
            else:
                # Explicit legacy anchors never fall through to the new type,
                # even if their occurrence is wrong or their quote is missing.
                row = SourceVisitSupplement.model_validate(raw)
        except ValidationError:
            spans = _literal_spans(source, raw.get("evidence", "")) if isinstance(raw, dict) and isinstance(raw.get("evidence"), str) else []
            reject(index, spans[0] if len(spans) == 1 else None)
            continue
        evidence_matches = _literal_matches(source, row.evidence)
        evidence_spans = [span for span, _ in evidence_matches]
        if isinstance(row, SourceVisitPurpose):
            span = _unique_purpose_action(source, row, evidence_matches[0]) if len(evidence_matches) == 1 else None
            quote_indices = []
        else:
            quotes = _literal_matches(source, row.source_quote)
            span, quote_indices = quotes[row.occurrence - 1] if row.occurrence <= len(quotes) else (None, [])
        parent = by_id.get(parent_ids[row.parent_index]) if row.parent_index < len(parent_ids) else None
        diagnostic_span = evidence_spans[0] if len(evidence_spans) == 1 else span
        if (not parent or parent.role != ActivityRole.PLANNED or parent.parent_mention_id
            or not parent.atomic_place_name or parent.day_index is None
            or len(set(parent_ids)) != len(parent_ids) or not span or not evidence_spans
            or source[parent.span_start:parent.span_end] != parent.raw_text):
            reject(index, diagnostic_span)
            continue
        selected = None
        name = source[span[0]:span[1]] if isinstance(row, SourceVisitPurpose) else row.source_quote
        if row.kind in {"VISIT", "ENTRY", "EXIT"}:
            gate = _gate_anchor(row, quote_indices) if row.kind != "VISIT" else None
            for evidence_span in evidence_spans:
                if not evidence_span[0] <= span[0] < span[1] <= evidence_span[1]:
                    continue
                if _cancelled_or_conditional(source, *span, row.evidence, row.optional):
                    continue
                local_left = max(source.rfind(mark, parent.span_end, span[0]) for mark in "\n。；;，,") + 1
                local_before = source[max(parent.span_end, local_left):span[0]].replace("**", "")
                if row.kind == "VISIT" and re.search(r"(?:(?:园|馆)内(?:有|设有|包括|收藏)|(?:介绍|说明)[^。；;，,]{0,15}(?:有|包括))\s*$", local_before):
                    continue
                if row.kind != "VISIT":
                    if gate is None or not _gate_direction(row, source, span[0], gate[1], evidence_span):
                        continue
                    name = gate[0]
                if _scope_parent(source, anchors, row, parent, roots, *span, explicit_kind=row.kind != "VISIT"):
                    selected = evidence_span
                    break
        elif (action_span := _purpose_supported(row)) is not None:
            purpose_roots = _shared_purpose_roots(source, parent, roots)
            for evidence_span, evidence_indices in evidence_matches:
                # The explicit revised wire accepts a parent-name anchor only
                # for its exact supplied visit, never another same-name day.
                if not (span == (parent.span_start, parent.span_end)
                    or evidence_span[0] <= span[0] < span[1] <= evidence_span[1]):
                    continue
                action = (evidence_indices[action_span[0]], evidence_indices[action_span[1] - 1] + 1)
                if _cancelled_or_conditional(source, *action, row.evidence, row.optional):
                    continue
                if _scope_parent(source, anchors, row, parent, purpose_roots, *action, explicit_kind=True):
                    selected = evidence_span
                    break
        if selected is None:
            reject(index, diagnostic_span)
            continue
        key = (parent.mention_id, row.kind, *span)
        old = existing.get(key)
        if old:
            if old.role not in {ActivityRole.PLANNED, ActivityRole.OPTIONAL, ActivityRole.REFERENCE} or (old.role == ActivityRole.OPTIONAL) != row.optional:
                reject(index, diagnostic_span)
            elif row.kind == "VISIT" and key not in response_visits.setdefault(parent.mention_id, []):
                response_visits[parent.mention_id].append(key)
            continue
        purpose_key = (parent.mention_id, row.kind, *selected)
        if row.kind in {"EXTERIOR_ONLY", "PICKUP_ONLY"} and purpose_key in purpose_evidence:
            continue
        same_occurrence = [mention for mention in roots
            if row.kind == "VISIT" and mention.day_index == parent.day_index
            and (mention.span_start, mention.span_end) == span and mention.atomic_place_name == name]
        if same_occurrence and (len(same_occurrence) != 1 or not row.optional
            or same_occurrence[0].role != ActivityRole.OPTIONAL
            or same_occurrence[0].mention_id in reconciled_ids):
            # The supplement may clarify where one optional visit belongs,
            # but cannot demote a main stop or resolve conflicting occurrences.
            reject(index, diagnostic_span)
            continue
        child = ProposedMention(mention_id=f"source-visit-{parent.mention_id}-{row.kind}-{span[0]}-{span[1]}",
            raw_text=source[span[0]:span[1]], span_start=span[0], span_end=span[1],
            role=ActivityRole.OPTIONAL if row.optional else ActivityRole.REFERENCE,
            day_index=parent.day_index, sequence_index=len(proposal.mentions) + index, atomic_place_name=name,
            category_hint=parent.category_hint, parent_mention_id=parent.mention_id,
            relation_type="INTERNAL_DETAIL", detail_kind=row.kind, role_evidence=source[selected[0]:selected[1]],
            role_evidence_start=selected[0], role_evidence_end=selected[1])
        if same_occurrence:
            # Keep the original occurrence's ID, role, source and metadata.
            # Only its validated parent relation and internal ordering change.
            original = same_occurrence[0]
            child = original.model_copy(update={field: getattr(child, field) for field in (
                "parent_mention_id", "relation_type", "detail_kind", "sequence_index",
                "role_evidence", "role_evidence_start", "role_evidence_end")})
            reconciled_ids.add(original.mention_id)
        additions.append(child)
        addition_origins[child.mention_id] = (index, diagnostic_span)
        existing[key] = child
        purpose_evidence.add(purpose_key)
        if row.kind == "VISIT":
            response_visits.setdefault(parent.mention_id, []).append(key)
    rejected_ids = set()
    for parent_id, response_order in response_visits.items():
        old_visits = sorted((m for m in proposal.mentions if m.parent_mention_id == parent_id
            and m.relation_type == "INTERNAL_DETAIL" and (m.detail_kind or "VISIT") == "VISIT"
            and m.role in {ActivityRole.PLANNED, ActivityRole.OPTIONAL, ActivityRole.REFERENCE}),
            key=lambda m: m.sequence_index)
        new_visits = [m for m in additions if m.parent_mention_id == parent_id and m.detail_kind == "VISIT"]
        if not old_visits or not new_visits:
            continue
        old_keys = [(parent_id, "VISIT", m.span_start, m.span_end) for m in old_visits]
        old_by_key = dict(zip(old_keys, old_visits, strict=True))
        if [key for key in response_order if key in old_by_key] != old_keys:
            # Missing/reversed old anchors cannot authorize a guessed insertion.
            for child in new_visits:
                reject(*addition_origins[child.mention_id])
                rejected_ids.add(child.mention_id)
            continue
        left, pending = -1, []
        for key in [*response_order, None]:
            if key is not None and key not in old_by_key:
                pending.append(existing[key])
                continue
            right = old_by_key[key].sequence_index if key else left + len(pending) + 1
            if right - left - 1 < len(pending):
                for child in pending:
                    reject(*addition_origins[child.mention_id])
                    rejected_ids.add(child.mention_id)
            else:
                for offset, child in enumerate(pending, 1):
                    child.sequence_index = left + offset
            left, pending = right, []
    additions = [child for child in additions if child.mention_id not in rejected_ids]
    # An order failure leaves the original independent optional untouched.
    reconciled_ids &= {child.mention_id for child in additions}
    retained = [mention for mention in proposal.mentions if mention.mention_id not in reconciled_ids]
    if len(retained) + len(additions) > MAX_TRIP_ACTIVITIES:
        raise InferenceProviderUnavailableError(INPUT_CAPACITY_EXCEEDED,
            provider_binding=proposal.binding, external_call_count=int(proposal.binding.get("external_calls", 0)))
    covered = {(m.span_start, m.span_end) for m in additions}
    removed = [d for d in proposal.diagnostics if d.category == "KNOWN_PLACE_UNCLASSIFIED"
        and (d.span_start, d.span_end) in covered]
    diagnostics = [d for d in proposal.diagnostics if d not in removed] + issues
    by_day = dict(proposal.unprocessed_by_day)
    for issue in removed:
        day = next((day for day, left, right in day_scopes
            if left <= issue.span_start < issue.span_end <= right), None)
        if day and by_day.get(day):
            by_day[day] -= 1
    for day, count in issue_days.items():
        by_day[day] = by_day.get(day, 0) + count
    return proposal.model_copy(update={"mentions": [*retained, *additions], "diagnostics": diagnostics,
        "unprocessed_count": max(0, proposal.unprocessed_count - len(removed)) + len(issues),
        "unprocessed_by_day": by_day})
