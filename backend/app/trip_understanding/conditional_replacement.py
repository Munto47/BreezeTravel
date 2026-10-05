"""Bind model-provided fallback meaning to existing literal visit occurrences."""
from __future__ import annotations

import re

from pydantic import Field, ValidationError

from app.trip_understanding.models import ActivityRole, SemanticDiagnostic, StrictModel


class SemanticReplacement(StrictModel):
    target_quote: str = Field(min_length=1, max_length=100)
    target_occurrence: int = Field(default=1, strict=True, ge=1, le=160)
    condition_quote: str = Field(min_length=1, max_length=80)
    evidence: str = Field(min_length=1, max_length=2000)


def is_replacement_reference(source, proposal, start, end):
    """An explicitly bound replacement can restate its default, not revisit it."""
    by_id = {m.mention_id: m for m in proposal.mentions}
    for alternative in proposal.mentions:
        target = by_id.get(alternative.replaces_mention_id)
        if target is None or not alternative.replacement_condition:
            continue
        left = max(source.rfind(mark, 0, alternative.span_start) for mark in '\n。；;！!？?') + 1
        if not (target.span_end <= left <= start < end <= alternative.span_start):
            continue
        before = source[left:start]
        after = source[end:alternative.span_start]
        right = min((p for mark in '\n。；;！!？?' if (p := source.find(mark, alternative.span_end)) >= 0), default=len(source))
        # The binder accepts a source-grounded condition within this clause.
        # Reuse that scope: models may quote the full replacement sentence.
        if (source[start:end] == target.raw_text and alternative.replacement_condition in source[left:right]
                and re.search(r'(?:把|将)\s*(?:这次|本次)?\s*$', before)
                and re.fullmatch(r'\s*(?:的(?:安排|行程|游览))?\s*(?:替换成|替换为|换成|换为)\s*', after)):
            return True
    return False


def is_implicit_alternative_reference(source, proposal, segment, quote, *, day_index=None):
    """A following clause can restate the same validated rainy-day alternative."""
    if not re.fullmatch(r'\s*(?:暂按晴天主线整理[，,]\s*)?雨天方案保留为备选[。；;\s]*', segment['text']):
        return False
    previous = max((m for m in proposal.mentions if not m.parent_mention_id and m.span_end <= segment['start']),
                   key=lambda m: m.span_end, default=None)
    if (previous is None or previous.role != 'OPTIONAL' or not previous.replaces_mention_id
            or not re.search(r'下雨|雨天', previous.replacement_condition or '')
            or previous.raw_text != quote or (day_index is not None and previous.day_index != day_index)):
        return False
    clause_end = min((p for mark in '\n。；;！!？?'
        if (p := source.find(mark, previous.span_end)) >= 0), default=len(source))
    return clause_end <= segment['start'] and not source[clause_end:segment['start']].strip('。；;\n\r \t')


def is_implicit_default_reference(source, proposal, segment, quote, *, day_index=None):
    """A narrow return-to-default sentence adds no named destination."""
    if not re.fullmatch(r'\s*(?:晴天|否则|不下雨时|没有下雨时|天气好的话)?[，,]?\s*仍(?:然)?(?:按|走)原(?:来|先|定)的?'
                        r'(?:公园)?(?:计划|安排|路线)(?:走|进行)?[。；;\s]*', segment['text']):
        return False
    previous = max((m for m in proposal.mentions if not m.parent_mention_id and m.span_end <= segment['start']),
                   key=lambda m: m.span_end, default=None)
    if previous is None or previous.replaces_mention_id is None:
        return False
    target = next((m for m in proposal.mentions if m.mention_id == previous.replaces_mention_id), None)
    # The validated replacement clause can describe what to do at its venue
    # after the venue name. The following default reference starts after that
    # clause, not necessarily immediately after the name.
    clause_end = min((p for mark in '\n。；;！!？?'
        if (p := source.find(mark, previous.span_end)) >= 0), default=len(source))
    if clause_end > segment['start']:
        return False
    between = source[clause_end:segment['start']]
    return (target is not None and (target.raw_text == quote or bool(re.fullmatch(
                r'原(?:来|先|定)的?(?:公园)?(?:计划|安排|路线)', quote)))
            and (day_index is None or target.day_index == day_index)
            and not between.strip('。；;\n\r \t'))


def bind_conditional_replacements(source, draft, mentions):
    """Never promote roles, guess a target, or let one invalid row erase others."""
    from app.trip_understanding.experience_inference import SourceAnchorIndex

    anchors = SourceAnchorIndex(source)
    by_id = {mention.mention_id: mention for mention in mentions}
    updates, diagnostics = {}, []
    for index, item in enumerate(draft.activities):
        raw = item.conditional_replacement
        if raw is None:
            continue
        alternative = by_id.get(f"activity-{index + 1}")
        try:
            row = SemanticReplacement.model_validate(raw)
            left, right = anchors.locate(row.evidence)
            # The complete evidence must identify one location in the source.
            try:
                anchors.locate(row.evidence, 2)
            except ValueError:
                pass
            else:
                raise ValueError("ambiguous evidence")
            target_span = anchors.locate(row.target_quote, row.target_occurrence)
            targets = [m for m in mentions if not m.parent_mention_id and m.atomic_place_name
                and target_span[0] <= m.span_start < m.span_end <= target_span[1]]
            if len(targets) != 1 or not alternative:
                raise ValueError("missing visit")
            target = targets[0]
            if (target.role != ActivityRole.PLANNED or alternative.role != ActivityRole.OPTIONAL
                or not alternative.atomic_place_name or alternative.parent_mention_id or target.day_index != alternative.day_index
                or target.day_index is None or target.mention_id == alternative.mention_id
                or target.choice_group_id or alternative.choice_group_id
                or not target.span_end <= alternative.span_start
                or not left <= alternative.span_start < alternative.span_end <= right):
                raise ValueError("incompatible visits")
            evidence = source[left:right]
            if evidence.count(row.condition_quote) != 1 or not re.match(r"^(?:如果|假如|要是|若).+", row.condition_quote):
                raise ValueError("condition not grounded")
            condition_start = left + evidence.index(row.condition_quote)
            if not target.span_end <= condition_start < alternative.span_start:
                raise ValueError("condition scope")
            clause_left = max(source.rfind(mark, 0, condition_start) for mark in "\n。；;！!？?") + 1
            clause_right = min((p for mark in "\n。；;！!？?" if (p := source.find(mark, alternative.span_end)) >= 0), default=len(source))
            clause = source[clause_left:clause_right]
            if right < clause_right or not re.search(r"改去|改成|改为|换成|换为|替换|替代", clause):
                raise ValueError("replacement not grounded")
            if re.search(r"取消|撤销|不(?:再|要)?(?:改|换|替)|不是|并非|不采用|不执行|作废|[“”「」>]", clause):
                raise ValueError("negated or quoted replacement")
            default_left = max(source.rfind(mark, 0, target.span_start) for mark in "\n。；;，,") + 1
            default_context = source[default_left:target.span_start]
            if re.search(r"如果|假如|要是|若|备选|方案|取消|不去|不再|参考|[“”「」>]", default_context):
                raise ValueError("no affirmative default")
            other_visits = [m for m in mentions if not m.parent_mention_id and m.atomic_place_name
                and m.mention_id not in {target.mention_id, alternative.mention_id}
                and target.span_end <= m.span_start < alternative.span_end]
            if any(condition_start <= m.span_start for m in other_visits):
                raise ValueError("multi-visit replacement")
            # An implicit 'change to' can refer only to the adjacent default.
            # With intervening visits, the condition must name the target again.
            condition_text = source[condition_start:clause_right]
            target_name, alternative_name = re.escape(target.atomic_place_name), re.escape(alternative.atomic_place_name or "")
            explicit_target = bool(re.search(r"(?:把|将)\s*(?:这次|本次)?\s*" + target_name
                + r"\s*(?:的(?:安排|行程|游览))?\s*(?:替换成|替换为|换成|换为)\s*" + alternative_name, condition_text)
                or re.search(r"(?:用|以)\s*" + alternative_name + r"\s*(?:替换|替代)\s*" + target_name, condition_text))
            # The evidence may quote just the replacement sentence. Its
            # explicit operator must then name the already anchored default;
            # implicit references still require evidence covering that visit.
            if target.span_start < left and not explicit_target:
                raise ValueError("default outside implicit evidence")
            before = source[condition_start:alternative.span_start]
            after = source[alternative.span_end:clause_right]
            implicit_operator = bool(re.search(r"(?:改去|改成|改为|换成|换为)\s*$", before)
                or re.search(r"(?:用|以)\s*$", before) and re.match(r"\s*(?:替换|替代)(?:[，,\s]|$)", after))
            adjacent_default = bool(re.fullmatch(r"[\s。；;]*", source[target.span_end:condition_start]))
            if not explicit_target and (other_visits or not implicit_operator or not adjacent_default):
                raise ValueError("replacement operator does not bind these visits")
            same_name = [m for m in mentions if not m.parent_mention_id and m.day_index == target.day_index
                and m.atomic_place_name == target.atomic_place_name and m.span_end <= condition_start]
            if len(same_name) != 1 and not (adjacent_default
                    and max(same_name, key=lambda m: m.span_end).mention_id == target.mention_id):
                raise ValueError("ambiguous repeated default")
            updates[alternative.mention_id] = alternative.model_copy(update={
                "replaces_mention_id": target.mention_id, "replacement_condition": row.condition_quote})
        except (ValidationError, ValueError):
            diagnostics.append(SemanticDiagnostic(category="CONDITIONAL_REPLACEMENT_UNRESOLVED",
                field=f"activities[{index}].conditional_replacement",
                span_start=alternative.span_start if alternative else None,
                span_end=alternative.span_end if alternative else None))
    return [updates.get(m.mention_id, m) for m in mentions], diagnostics
