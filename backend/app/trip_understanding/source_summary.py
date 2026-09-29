"""Recognize repeated day summaries without collapsing actual return visits."""
from __future__ import annotations

import re

from app.trip_understanding.models import ActivityRole


def _conditional_replacements(source, mentions):
    """Bind an explicit fallback to one earlier affirmative visit, never a POI.

    Unknown/implicit substitutions remain unselected. This recognizes only
    literal source operators after the model has supplied both named visits.
    """
    updates = {}
    for alternative in mentions:
        if (alternative.role != ActivityRole.OPTIONAL or not alternative.atomic_place_name
                or alternative.parent_mention_id or alternative.replaces_mention_id):
            continue
        left = max(source.rfind(mark, 0, alternative.span_start) for mark in '\n。；;') + 1
        prefix = source[left:alternative.span_start]
        condition = re.match(r'\s*((?:如果|假如|要是|若)[^，,。；;\n]{1,30})[，,]\s*(?:就)?(?:把|将)', prefix)
        if not condition or re.search(r'取消|撤销|不采用|不替换', prefix):
            continue
        matches = []
        for original in mentions:
            name = original.atomic_place_name
            if (not name or original.day_index != alternative.day_index or original.parent_mention_id
                    or original.span_end >= left or original.role not in {ActivityRole.PLANNED, ActivityRole.OPTIONAL}):
                continue
            if not re.fullmatch(re.escape(name) + r'\s*(?:替换成|换成|替换为|换为)\s*', prefix[condition.end():]):
                continue
            line_start = source.rfind('\n', 0, original.span_start) + 1
            line_prefix = source[line_start:original.span_start]
            if re.search(r'如果|假如|若|要是|备选|方案|取消|不去|不再|参考|[“”「」>]', line_prefix):
                continue
            if not re.search(r'(?:先去|然后去|再去|前往|去)\s*$', line_prefix):
                continue
            if source[original.span_start:original.span_end] != original.raw_text:
                continue
            matches.append(original)
        if len(matches) != 1:
            continue
        original = matches[0]
        # Do not partially reinterpret a multi-visit branch as a single stop.
        groups = {value for value in (alternative.choice_group_id, original.choice_group_id) if value}
        if any(item.choice_group_id in groups and item.mention_id not in
               {original.mention_id, alternative.mention_id} for item in mentions):
            continue
        clear_choice = dict(choice_group_id=None, branch_id=None, branch_label=None, choice_group_selectable=False)
        updates[original.mention_id] = dict(role=ActivityRole.PLANNED, **clear_choice)
        updates[alternative.mention_id] = dict(replaces_mention_id=original.mention_id,
            replacement_condition=condition[1], **clear_choice)
    return updates


def explicit_source_relations(source, proposal):
    """Repair only literal choice/entrance relations, preserving source records.

    The model still supplies names and spans. No POI, coordinate or visit is
    invented here, and genuine return visits are not deduplicated.
    """
    mentions = []
    replacements = _conditional_replacements(source, proposal.mentions)
    for item in proposal.mentions:
        if source[item.span_start:item.span_end] != item.raw_text:
            mentions.append(item)
            continue
        begin = source.rfind('\n', 0, item.span_start) + 1
        end = source.find('\n', item.span_end)
        line = source[begin:end if end >= 0 else len(source)].replace('*', '')
        name = item.atomic_place_name
        updates = {}
        if name and item.role == ActivityRole.PLANNED:
            # A named restaurant OR self-provided food is an unselected choice.
            if item.category_hint == '餐饮' and re.search(r'或(?:者)?自带(?:简餐|便当|午餐|食物)', line):
                updates['role'] = ActivityRole.OPTIONAL
            for parent in proposal.mentions:
                if (parent.mention_id == item.mention_id or parent.day_index != item.day_index
                    or parent.role != ActivityRole.PLANNED or not parent.atomic_place_name):
                    continue
                parent_name = parent.atomic_place_name
                entrance = re.search(r'从\s*' + re.escape(name) + r'\s*(?:进入|进)\s*' + re.escape(parent_name), line)
                transit = (item.category_hint == '交通节点' and '优先乘坐' in line
                    and re.search(r'或\s*\d+\s*号线', line)
                    and re.search(r'(?:去|前往)' + re.escape(parent_name), line))
                if entrance or transit:
                    updates.update(parent_mention_id=parent.mention_id, relation_type='INTERNAL_DETAIL')
                    if entrance:
                        updates['detail_kind'] = 'ENTRY'
                    if transit:
                        updates['role'] = ActivityRole.OPTIONAL
                    break
        if (name and item.role == ActivityRole.OPTIONAL and '可顺路' in line
            and not re.search(r'如果|若|时间(?:充裕|允许|足够)|有(?:时间|空)|二选一|或者|否则', line)):
            updates['role'] = ActivityRole.PLANNED
        updates.update(replacements.get(item.mention_id, {}))
        mentions.append(item.model_copy(update=updates) if updates else item)
    # A rejected unselected-choice proposal can describe this same explicit
    # default/replacement sentence. Retire only that fully accounted-for issue;
    # an overlapping paragraph or another choice stays unfinished.
    by_id = {item.mention_id: item for item in mentions}
    resolved_days = []
    diagnostics = []
    for issue in proposal.diagnostics:
        resolved = None
        if issue.category == 'CHOICE_GROUP_UNRESOLVED' and issue.span_start is not None and issue.span_end is not None:
            fragment = source[issue.span_start:issue.span_end]
            for alternative in mentions:
                original = by_id.get(alternative.replaces_mention_id)
                if (original is None or not original.atomic_place_name or not alternative.replacement_condition
                        or not alternative.atomic_place_name or alternative.mention_id not in replacements):
                    continue
                if not (issue.span_start <= alternative.span_start < alternative.span_end <= issue.span_end):
                    continue
                pattern = (r'\s*' + re.escape(alternative.replacement_condition) + r'[，,]\s*(?:就)?(?:把|将)\s*'
                    + re.escape(original.atomic_place_name) + r'\s*(?:替换成|换成|替换为|换为)\s*'
                    + re.escape(alternative.atomic_place_name) + r'(?:[，,]\s*(?:二者只选一个|两者只选一个|二选一))?[。.!！]?\s*')
                if re.fullmatch(pattern, fragment):
                    resolved = original.day_index
                    break
        if resolved is None:
            diagnostics.append(issue)
        else:
            resolved_days.append(resolved)
    by_day = dict(proposal.unprocessed_by_day)
    for day in resolved_days:
        if day in by_day:
            by_day[day] = max(0, by_day[day] - 1)
    return proposal.model_copy(update={'mentions': mentions, 'diagnostics': diagnostics,
        'unprocessed_count': max(0, proposal.unprocessed_count - len(resolved_days)), 'unprocessed_by_day': by_day})


def summary_references(source, activities):
    def is_summary(item):
        mention = item.compiled.mention
        line = source[source.rfind("\n", 0, mention.span_start) + 1:mention.span_start]
        return bool(re.match(r"^[\s*#>]*(?:主打|主题|行程概览|今日亮点)\s*[：:]", line))

    visits = {(item.compiled.mention.day_index, item.place.canonical_place_id)
        for item in activities if item.place and not is_summary(item)
        and item.compiled.mention.role == ActivityRole.PLANNED}
    references = set()
    for item in activities:
        if item.place and is_summary(item) and (item.compiled.mention.day_index,
                item.place.canonical_place_id) in visits:
            references.add(item.compiled.mention.mention_id)
    return references
