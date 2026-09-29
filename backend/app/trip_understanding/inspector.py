"""Actionable issues, independent of background checks and their rule count."""
from typing import Literal
from uuid import NAMESPACE_URL, uuid5
from pydantic import Field
from app.trip_understanding.models import StrictModel, PublicTripCheckItem


class InspectorIssue(StrictModel):
    issue_id: str
    kind: Literal['PLACE', 'SOURCE', 'CHECK', 'MEAL', 'ALTERNATIVE', 'LODGING']
    category: Literal['DECISION', 'OPTIONAL']
    severity: Literal['INFO', 'WARNING', 'ERROR'] = 'INFO'
    disposition: Literal['OPEN', 'IGNORED'] = 'OPEN'
    title: str
    message: str
    day_index: int | None = None
    target_day_ids: list[str] = Field(default_factory=list)
    target_visit_ids: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    input_version: str
    input_fingerprint: str
    check: PublicTripCheckItem | None = None


def build_issues(result, checks=None, *, input_version=''):
    issues = []
    pending = {}
    def add(kind, key, title, message, day_index=None, visits=(), category='DECISION', actions=()):
        issue_id = uuid5(NAMESPACE_URL, f'inspector:{kind}:{key}').hex
        fingerprint = uuid5(NAMESPACE_URL, str((key, title, message, tuple(visits)))).hex
        item = InspectorIssue(issue_id=issue_id, kind=kind, category=category, title=title,
            message=message, day_index=day_index, target_visit_ids=list(visits),
            target_day_ids=[result.days[day_index].day_id] if day_index is not None else [],
            severity='WARNING' if category=='DECISION' else 'INFO', actions=list(actions),
            input_version=input_version, input_fingerprint=fingerprint)
        issues.append(item)
        return item
    for index, day in enumerate(result.days):
        for card in day.activities:
            if card.status != 'READY':
                item=add('PLACE', card.visit_id, card.name, '没有有效的地图匹配，请补充名称或重新搜索。',
                    index, [card.visit_id], actions=['SEARCH_PLACE', 'RETRY_MATCH'])
                pending[card.activity_token]=item
        if day.unprocessed_count:
            add('SOURCE', day.day_id, f'Day {index+1} · 原文待整理',
                '核对尚未加入的原文内容；可恢复为安排或保留为备注。', index,
                actions=['RESTORE_SOURCE', 'KEEP_NOTE'])
        groups = {item.choice_group_token or item.alternative_id for item in day.alternatives
            if not any(selection.choice_group_token == item.choice_group_token for selection in day.choice_selections)}
        if groups:
            # A selection is one action, not one issue per branch/candidate.
            for group in sorted(groups):
                add('ALTERNATIVE', f'{day.day_id}:{group}', f'Day {index+1} · 原文备选方案',
                    '查看备选内容与插入位置，选择后才加入行程。', index, category='OPTIONAL', actions=['CHOOSE_ALTERNATIVE'])
        for position, slot in enumerate(day.meal_slots):
            if slot.selection_status != 'SELECTED':
                add('MEAL', f'{day.day_id}:{position}', f'Day {index+1} · 用餐选择',
                    slot.preference_text or '可按需选择附近餐饮。', index, category='OPTIONAL', actions=['CHOOSE_MEAL'])
    for hotel in result.pending_lodgings:
        add('LODGING', hotel.lodging_id, '原文住宿安排', '补充酒店地点与住宿范围。', actions=['CONFIRM_LODGING'])
    for check in (checks.all_items or checks.items) if checks else []:
        if check.basis_status == 'NEEDS_RECHECK':
            continue
        roots = [pending[token] for token in check.affected_activity_tokens if token in pending]
        if check.issue_type == 'PLACE_CONFIRMATION_REQUIRED' and roots:
            continue
        if check.depends_on_routes and roots:
            for root in roots:
                if '相邻路线将在地点确定后更新。' not in root.message:
                    root.message += ' 相邻路线将在地点确定后更新。'
            continue
        issues.append(InspectorIssue(issue_id=check.issue_id, kind='CHECK',
            category='OPTIONAL' if check.label=='可以更好' else 'DECISION',
            severity='ERROR' if check.label=='必须调整' else 'WARNING',
            title=check.title, message=check.message, target_day_ids=check.target_day_ids,
            target_visit_ids=check.target_visit_ids, input_version=input_version,
            input_fingerprint=check.input_fingerprint, check=check,
            actions=['PREVIEW_CHANGE', 'IGNORE'] if check.can_preview else ['LOCATE', 'IGNORE']))
    unique = {item.issue_id:item for item in issues}
    for item in unique.values():
        saved=result.issue_dispositions.get(item.issue_id)
        if saved and saved.get('input_fingerprint')==item.input_fingerprint:
            item.disposition='IGNORED'
    return list(unique.values())
