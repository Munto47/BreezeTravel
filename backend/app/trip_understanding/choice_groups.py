"""Source-bound, unselected two-way choices within the existing semantic answer.

No place names, roles or dates are extracted here. Members must already exist
as separately validated optional visits. Opaque public selection is downstream.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field

from app.trip_understanding.models import ActivityRole, SemanticDiagnostic, StrictModel


class SemanticChoiceBranch(StrictModel):
    activity_indices: list[Annotated[int, Field(strict=True, ge=0, lt=160)]] = Field(min_length=1, max_length=160)


class SemanticChoiceGroup(StrictModel):
    scope_quote: str = Field(min_length=1, max_length=2000)
    occurrence: int = Field(default=1, strict=True, ge=1, le=160)
    status: Literal["UNSELECTED"] = "UNSELECTED"
    branches: list[SemanticChoiceBranch] = Field(min_length=2, max_length=2)


def _identity(source, activity):
    from app.trip_understanding.experience_inference import SourceAnchorIndex

    anchors = SourceAnchorIndex(source)
    try:
        left, right = anchors.locate(activity.source_quote, activity.occurrence)
    except ValueError:
        return None
    span = anchors.place_span(left, right, activity.place_name) if activity.place_name else None
    return (left + span[0], left + span[1], activity.day_index, activity.role) if span else None


def remap_choice_groups(source, original, updated):
    """Rebind reply indices after recovery; an ambiguous visit is never guessed."""
    positions = {}
    for index, activity in enumerate(updated.activities):
        identity = _identity(source, activity)
        if identity is not None:
            positions.setdefault(identity, []).append(index)
    groups = []
    pending = list(updated.unprocessed_quotes)
    for group in original.choice_groups:
        branches = []
        for branch in group.branches:
            indices = []
            for index in branch.activity_indices:
                identity = _identity(source, original.activities[index]) if index < len(original.activities) else None
                matches = positions.get(identity, [])
                if len(matches) != 1:
                    break
                indices.append(matches[0])
            if len(indices) != len(branch.activity_indices):
                break
            branches.append(branch.model_copy(update={"activity_indices": indices}))
        if len(branches) == 2:
            groups.append(group.model_copy(update={"branches": branches}))
        elif group.scope_quote in source and group.scope_quote not in pending and len(pending) < 80:
            pending.append(group.scope_quote)
    return updated.model_copy(update={"choice_groups": groups, "unprocessed_quotes": pending})


def _choice_gap(source, left, right):
    """An 'or' inside one place's parentheses describes that visit, not two branches."""
    from app.trip_understanding.experience_inference import _markdown_visible

    gap = _markdown_visible(source[left:right])[0]
    pairs = re.compile(r"\([^()\n]*\)|（[^（）\n]*）|【[^【】\n]*】|\[[^\[\]\n]*\]")
    while pairs.search(gap):
        gap = pairs.sub(" ", gap)
    # Name wrappers can straddle the member boundary (【A】或【B】).
    # Unmatched parentheses, unlike wrappers, leave the purpose scope unknown.
    return "" if re.search(r"[()（）]", gap) else gap.translate(str.maketrans("【】[]", "    "))


def _source_allows_choice(source, left, right, branches, day):
    from app.trip_understanding.experience_inference import _markdown_visible
    from app.trip_understanding.guide_choices import _DAY_HEADING, _day_number

    visible = _markdown_visible(source[left:right])[0]
    if re.search(r"(?:放弃|取消|替换|删掉)[^。！？；;\n]{0,15}(?:第[一二两三四五六七八九十\d]+天|Day\s*\d+|前一天|昨天)", visible, re.I):
        return False
    for branch in branches:
        local = _markdown_visible(source[branch[0].span_start:branch[-1].span_end])[0]
        if re.search(r"可选|备选|二选一|如果|假如|若有|①|②|③", local):
            return False
    gap = _choice_gap(source, branches[0][-1].span_end, branches[1][0].span_start)
    # The operator must separate branches, not be part of a name or "或许".
    operator = bool(re.search(r"或(?:者)?(?!许|然)|要么", gap))
    explicit_binary = bool(re.search(r"二选一|任选其一|择一|选一个", visible) and re.search(r"[/／|｜]|方案|版本", gap))
    conditional_binary = bool(re.search(r"如果|假如|要是|若", visible) and re.search(r"否则|不然", gap))
    if not (operator or explicit_binary or conditional_binary):
        return False
    headings = list(_DAY_HEADING.finditer(source))
    containing = [heading for heading in headings if heading.start() <= left]
    if containing and _day_number(containing[-1]["label"]) != day:
        return False
    if any(left < heading.start() < right for heading in headings):
        return False
    # Include the full statement, so a model cannot crop off its negation.
    context_left = max(source.rfind(mark, 0, left) for mark in "\n。！？；;") + 1
    context_right = min((pos for mark in "\n。！？；;" if (pos := source.find(mark, right)) >= 0), default=len(source))
    context = _markdown_visible(source[context_left:context_right])[0]
    if re.search(r"取消|撤销|不去|不再|不采用|不选择|不执行|作废|不是.{0,8}二选一|无需.{0,8}二选一|"
                 r"(?:已|已经|最终|确定|决定).{0,8}(?:选|去|采用|执行)", context):
        return False
    # A later explicit decision naming a member settles this source choice.
    day_end = next((heading.start() for heading in headings if heading.start() > right), len(source))
    tail = _markdown_visible(source[right:day_end])[0]
    names = [item.atomic_place_name for branch in branches for item in branch]
    if any(re.search(r"(?:最终|已经|已选|决定|确定)[^。！？；;\n]{0,35}" + re.escape(name), tail) for name in names):
        return False
    return True


def bind_choice_groups(source, original, mentions):
    """Validate typed groups and clarify only a complete explicit inline 'or'."""
    from app.trip_understanding.experience_inference import SourceAnchorIndex
    from app.trip_understanding.guide_choices import explicit_choice_branches

    anchors = SourceAnchorIndex(source)
    source_branches = explicit_choice_branches(source)
    by_identity = {}
    for mention in mentions:
        key = (mention.span_start, mention.span_end, mention.day_index, mention.role)
        by_identity.setdefault(key, []).append(mention)
    assignments = {}
    diagnostics = []
    occupied = set()

    def accept(left, right, branches):
        flat = [item for branch in branches for item in branch]
        ids = [item.mention_id for item in flat]
        days = {item.day_index for item in flat}
        if (len(branches) != 2 or not all(branches) or len(ids) != len(set(ids))
                or len(days) != 1 or None in days or occupied.intersection(ids)
                or any(item.role != ActivityRole.OPTIONAL or not item.atomic_place_name or item.parent_mention_id for item in flat)
                or any(not (left <= item.span_start < item.span_end <= right) for item in flat)
                or any([item.span_start for item in branch] != sorted(item.span_start for item in branch) for branch in branches)
                or branches[0][-1].span_end > branches[1][0].span_start):
            return False
        # A proposed branch must not silently skip another visit inside its scope.
        actual = {item.mention_id for item in mentions if left <= item.span_start < item.span_end <= right
                  and item.role in {ActivityRole.PLANNED, ActivityRole.OPTIONAL} and not item.parent_mention_id}
        if actual != set(ids) or not _source_allows_choice(source, left, right, branches, next(iter(days))):
            return False
        # Derive labels from the same verified source scopes as OPTIONAL roles,
        # not from incoming mention metadata. Every member of each branch must
        # belong to one distinct heading in the same source group and day.
        labels = ["方案一", "方案二"]
        heading_matches = [[heading for heading in source_branches
            if all(heading.day == item.day_index and heading.start <= item.span_start < item.span_end <= heading.end
                   for item in branch)] for branch in branches]
        if all(len(matches) == 1 for matches in heading_matches):
            first, second = (matches[0] for matches in heading_matches)
            if (first.group_id == second.group_id and first.branch_id != second.branch_id and first.label != second.label
                    and sum(heading.group_id == first.group_id for heading in source_branches) == 2):
                labels = [first.label, second.label]
        group_id = f"choice-{left}-{right}"
        for index, branch in enumerate(branches):
            for item in branch:
                assignments[item.mention_id] = {"choice_group_id": group_id,
                    "branch_id": f"{group_id}-{index + 1}", "branch_label": labels[index],
                    "choice_group_selectable": True}
        occupied.update(ids)
        return True

    for index, group in enumerate(original.choice_groups):
        left = right = None
        try:
            left, right = anchors.locate(group.scope_quote, group.occurrence)
            branches = []
            for branch in group.branches:
                rows = []
                for activity_index in branch.activity_indices:
                    activity = original.activities[activity_index]
                    matches = by_identity.get(_identity(source, activity), [])
                    if len(matches) != 1:
                        raise ValueError("ambiguous member")
                    rows.append(matches[0])
                branches.append(rows)
            if not accept(left, right, branches):
                raise ValueError("unsupported choice")
        except (ValueError, IndexError):
            diagnostics.append(SemanticDiagnostic(category="CHOICE_GROUP_UNRESOLVED", field=f"choice_groups[{index}]",
                span_start=left, span_end=right))

    # Legacy answers have no group field. Clarify a source-complete inline
    # binary only; never infer conditions from OPTIONAL alone or create visits.
    if not original.choice_groups:
        for match in re.finditer(r"[^\n。！？；;]+", source):
            rows = sorted((item for item in mentions if match.start() <= item.span_start < item.span_end <= match.end()
                           and item.role in {ActivityRole.PLANNED, ActivityRole.OPTIONAL} and not item.parent_mention_id),
                          key=lambda item: item.span_start)
            if len(rows) < 2 or any(item.choice_group_id for item in rows):
                continue
            if len(rows) == 2:
                accept(match.start(), match.end(), [[rows[0]], [rows[1]]])
            elif (all(item.role == ActivityRole.OPTIONAL for item in rows) and any(
                    re.search(r"或(?:者)?(?!许|然)|要么", _choice_gap(source, first.span_end, second.span_start))
                    for first, second in zip(rows, rows[1:]))):
                # A+B or C may mean two different branch structures. An old
                # flat answer cannot safely establish the multi-stop group.
                diagnostics.append(SemanticDiagnostic(category="CHOICE_GROUP_UNRESOLVED", field="choice_groups",
                    span_start=match.start(), span_end=match.end()))
    # Existing heading-derived groups can also be checked from their actual
    # source. Their tokens alone never authorize adopting the whole branch.
    from app.trip_understanding.guide_choices import _DAY_HEADING
    for group_id in dict.fromkeys(item.choice_group_id for item in mentions if item.choice_group_id):
        members = [item for item in mentions if item.choice_group_id == group_id and item.role == ActivityRole.OPTIONAL]
        if not members or any(item.mention_id in assignments for item in members):
            continue
        branch_ids = list(dict.fromkeys(item.branch_id for item in members))
        headings = [heading for heading in _DAY_HEADING.finditer(source) if heading.start() <= min(item.span_start for item in members)]
        if len(branch_ids) == 2 and None not in branch_ids and headings:
            accept(headings[-1].start(), max(item.span_end for item in members),
                   [[item for item in members if item.branch_id == branch_id] for branch_id in branch_ids])
    unresolved = {item.choice_group_id for item in mentions if item.choice_group_id and item.mention_id not in assignments}
    for group_id in unresolved:
        members = [item for item in mentions if item.choice_group_id == group_id]
        diagnostics.append(SemanticDiagnostic(category="CHOICE_GROUP_UNRESOLVED", field="choice_groups",
            span_start=min(item.span_start for item in members), span_end=max(item.span_end for item in members)))
    return [item.model_copy(update=assignments[item.mention_id]) if item.mention_id in assignments else item for item in mentions], diagnostics
