"""Validate source-bound additions without granting edits to existing visits.

No answer labels, provider calls or persistence belong here. The worker must
check its source and base version again inside the publication transaction.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, ValidationError

from app.trip_understanding.experience_inference import (
    SemanticActivity, SemanticDraft, SourceAnchorValidationError, _proposal_from_live_draft,
    SourceAnchorIndex, _is_parent_visit_detail, _validated_internal_parent,
)
from app.trip_understanding.models import (
    MAX_TRIP_ACTIVITIES, ActivityRole, PipelineOutput, ProposedMention, SourceSemanticPlan,
    SourceCorrectionSuggestionView, StrictModel, TripDayView, UserFacingTripResult,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline, _public_source_detail, _source_detail_order
from app.trip_understanding.source_summary import explicit_source_relations
from app.trip_understanding.source_occurrence import source_occurrence_id
from app.trip_understanding.source_visit_supplement import apply_source_visit_supplement, _cancelled_or_conditional


class AddSourceVisit(StrictModel):
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    operation: Literal["ADD_VISIT", "ADD_ALTERNATIVE"]
    activity: SemanticActivity
    # Position is relative to the saved version, never a guessed numeric slot.
    after_visit_id: str | None = None
    before_visit_id: str | None = None


class AddSourceDetail(StrictModel):
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    operation: Literal["ADD_DETAIL"]
    parent_visit_id: str = Field(min_length=1, max_length=80)
    details: list[Any] = Field(min_length=1, max_length=MAX_TRIP_ACTIVITIES)


class SuggestSourceCorrection(StrictModel):
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    operation: Literal["SUGGEST_CORRECTION"]
    target_visit_id: str = Field(min_length=1, max_length=80)
    source_quote: str = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class AcceptedSupplement:
    operation: AddSourceVisit | AddSourceDetail | SuggestSourceCorrection
    mentions: tuple[ProposedMention, ...] = ()


@dataclass
class SupplementValidation:
    accepted: list[AcceptedSupplement] = field(default_factory=list)
    # Only operation IDs and stable reasons; never copy source into logs.
    rejected: list[dict[str, str]] = field(default_factory=list)


@dataclass
class SupplementPatch:
    result: UserFacingTripResult
    validation: SupplementValidation
    resolved_additions: PipelineOutput | None
    changed_days: list[str]
    routes_changed: bool = False


def _has_existing_internal_parent(source: str, root: ProposedMention, preceding: list[ProposedMention]) -> bool:
    if _is_parent_visit_detail(source, root.atomic_place_name, root.span_start, root.span_end, root.day_index, preceding):
        return True
    parents = [mention for mention in preceding if not mention.parent_mention_id
        and mention.atomic_place_name and mention.day_index == root.day_index and mention.span_end <= root.span_start]
    if not parents:
        return False
    parent = max(parents, key=lambda mention: mention.span_end)
    evidence = source[parent.span_start:root.span_end]
    if len(evidence) > 500:
        return False
    candidate = SemanticActivity(source_quote=root.raw_text, place_name=root.atomic_place_name,
        role=root.role, day_index=root.day_index, parent_source_quote=parent.atomic_place_name, role_evidence=evidence)
    return _validated_internal_parent(source, SourceAnchorIndex(source), candidate, root.span_start, root.span_end,
        root.day_index, preceding, None) is not None


def validate_supplement(source: str, original: SourceSemanticPlan,
                        current: UserFacingTripResult, rows: list[Any]) -> SupplementValidation:
    if original.source_hash != hashlib.sha256(source.encode("utf-8")).hexdigest():
        raise ValueError("supplement source does not match its base")
    if len(rows) > MAX_TRIP_ACTIVITIES:
        raise ValueError("supplement exceeds operation capacity")
    outcome = SupplementValidation()
    cards = {card.visit_id: (day_index, card) for day_index, day in enumerate(current.days, 1) for card in day.activities}
    detail_parents = {**cards, **{card.alternative_id: (day_index, card)
        for day_index, day in enumerate(current.days, 1) for card in day.alternatives}}
    all_public = [card for day in current.days for card in [*day.activities, *day.alternatives]] + list(current.lodging_constraints)
    occupied = {card.source_occurrence_id for card in all_public if card.source_occurrence_id}
    occupied.update(value for mention in original.mentions if (value := source_occurrence_id(source, mention)))
    abandoned = {value for decision in current.visit_decisions.values() for value in decision.source_occurrence_ids}
    legacy_abandoned_names = {decision.name for decision in current.visit_decisions.values() if not decision.source_occurrence_ids}
    by_occurrence: dict[str, list[ProposedMention]] = {}
    for mention in original.mentions:
        if identity := source_occurrence_id(source, mention):
            by_occurrence.setdefault(identity, []).append(mention)
    ids = Counter(row.get("operation_id") for row in rows if isinstance(row, dict) and isinstance(row.get("operation_id"), str))
    working = original.model_copy(deep=True)
    new_count = 0

    def reject(op_id: str, reason: str) -> None:
        outcome.rejected.append({"operation_id": op_id, "reason": reason})

    for index, raw in enumerate(rows):
        op_id = raw.get("operation_id", f"row-{index}") if isinstance(raw, dict) else f"row-{index}"
        if not isinstance(op_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", op_id) is None:
            op_id = f"row-{index}"
        if ids[op_id] > 1:
            reject(op_id, "DUPLICATE_OPERATION_ID")
            continue
        try:
            kind = raw.get("operation") if isinstance(raw, dict) else None
            model = {"ADD_VISIT": AddSourceVisit, "ADD_ALTERNATIVE": AddSourceVisit,
                "ADD_DETAIL": AddSourceDetail, "SUGGEST_CORRECTION": SuggestSourceCorrection}.get(kind) if isinstance(kind, str) else None
            if model is None:
                reject(op_id, "OPERATION_NOT_ALLOWED")
                continue
            operation = model.model_validate(raw)
        except ValidationError:
            reject(op_id, "INVALID_OPERATION")
            continue
        if isinstance(operation, SuggestSourceCorrection):
            if operation.target_visit_id not in cards:
                reject(op_id, "TARGET_CHANGED")
            elif source.count(operation.evidence) != 1 or operation.evidence.count(operation.source_quote) != 1:
                reject(op_id, "SOURCE_NOT_UNIQUE")
            else:
                # A suggestion never authorizes a rename, replacement or role change.
                outcome.accepted.append(AcceptedSupplement(operation))
            continue
        if isinstance(operation, AddSourceDetail):
            target = detail_parents.get(operation.parent_visit_id)
            if target is None:
                reject(op_id, "TARGET_CHANGED")
                continue
            occurrence = target[1].source_occurrence_id
            parents = [mention for mention in by_occurrence.get(occurrence, []) if not mention.parent_mention_id]
            if occurrence in abandoned or target[1].name in legacy_abandoned_names:
                reject(op_id, "USER_DECISION")
                continue
            if len(parents) != 1:
                reject(op_id, "PARENT_SOURCE_UNKNOWN")
                continue
            # The model cannot choose a different parent by putting an index
            # inside a detail. Bind its supplied visit ID to this exact source.
            details = [dict(row, parent_index=0) if isinstance(row, dict) and "parent_index" not in row else {}
                for row in operation.details]
            changed = apply_source_visit_supplement(source, working, details, parent_ids=[parents[0].mention_id])
            old = {mention.mention_id: mention for mention in working.mentions}
            if any(mention.mention_id in old and mention != old[mention.mention_id] for mention in changed.mentions):
                reject(op_id, "EXISTING_VISIT_CHANGE")
                continue
            additions = tuple(mention for mention in changed.mentions if mention.mention_id not in old)
            if len(changed.diagnostics) > len(working.diagnostics):
                reject(op_id, "DETAIL_PARTIALLY_REJECTED" if additions else "DETAIL_REJECTED")
            if additions:
                outcome.accepted.append(AcceptedSupplement(operation, additions))
                working = working.model_copy(update={"mentions": [*working.mentions, *additions]})
                new_count += len(additions)
            elif len(changed.diagnostics) <= len(working.diagnostics):
                reject(op_id, "ALREADY_EXTRACTED")
            continue
        activity = operation.activity
        expected_role = ActivityRole.PLANNED if operation.operation == "ADD_VISIT" else ActivityRole.OPTIONAL
        if activity.role != expected_role or activity.parent_source_quote is not None:
            reject(op_id, "ROLE_NOT_ALLOWED")
            continue
        if activity.day_index is None:
            reject(op_id, "DAY_REQUIRED")
            continue
        anchors = [cards.get(value) for value in (operation.after_visit_id, operation.before_visit_id) if value is not None]
        if any(anchor is None or anchor[0] != activity.day_index for anchor in anchors):
            reject(op_id, "POSITION_CHANGED")
            continue
        daily = current.days[activity.day_index - 1].activities if activity.day_index <= len(current.days) else []
        if operation.operation == "ADD_VISIT" and daily and not anchors:
            reject(op_id, "POSITION_REQUIRED")
            continue
        if len(anchors) == 2:
            if daily.index(anchors[1][1]) != daily.index(anchors[0][1]) + 1:
                reject(op_id, "POSITION_CHANGED")
                continue
        try:
            proposed = _proposal_from_live_draft(source, SemanticDraft(destination=original.destination_name, activities=[activity]))
        except SourceAnchorValidationError:
            reject(op_id, "SOURCE_REJECTED")
            continue
        roots = [mention for mention in proposed.mentions if not mention.parent_mention_id]
        if len(roots) != 1 or roots[0].role != expected_role:
            reject(op_id, "ROLE_NOT_ALLOWED")
            continue
        root = roots[0]
        identity = source_occurrence_id(source, root)
        if not identity:
            reject(op_id, "SOURCE_NOT_UNIQUE")
            continue
        name_start = root.span_start + root.raw_text.index(root.atomic_place_name)
        name_end = name_start + len(root.atomic_place_name)
        if (_cancelled_or_conditional(source, name_start, name_end, root.raw_text, expected_role == ActivityRole.OPTIONAL)
                or _has_existing_internal_parent(source, root, working.mentions)):
            reject(op_id, "ROLE_NOT_ALLOWED")
            continue
        if identity in abandoned or root.atomic_place_name in legacy_abandoned_names:
            reject(op_id, "USER_DECISION")
            continue
        if identity in occupied:
            reject(op_id, "ALREADY_EXTRACTED")
            continue
        if len(original.mentions) + new_count + len(proposed.mentions) > MAX_TRIP_ACTIVITIES:
            reject(op_id, "CAPACITY_EXCEEDED")
            continue
        # Single-row parsing starts numbering at zero; model operation IDs
        # may also be reused in a later answer. Bind IDs to the source visit.
        new_ids = {mention.mention_id: f"supplement:{identity}:{mention.mention_id}" for mention in proposed.mentions}
        next_sequence = max((mention.sequence_index for mention in working.mentions), default=-1) + 1
        additions = tuple(mention.model_copy(update={"mention_id": new_ids[mention.mention_id],
            "parent_mention_id": new_ids.get(mention.parent_mention_id), "sequence_index": next_sequence + offset})
            for offset, mention in enumerate(proposed.mentions))
        related = explicit_source_relations(source, working.model_copy(update={"mentions": [*working.mentions, *additions]}))
        additions = tuple(mention for mention in related.mentions if mention.mention_id in set(new_ids.values()))
        # Relation repair cannot grant a different role to a requested addition.
        related_roots = [mention for mention in additions if not mention.parent_mention_id]
        if len(related_roots) != 1 or related_roots[0].role != expected_role:
            reject(op_id, "ROLE_NOT_ALLOWED")
            continue
        outcome.accepted.append(AcceptedSupplement(operation, additions))
        occupied.add(identity)
        new_count += len(additions)
        working = working.model_copy(update={"mentions": [*working.mentions, *additions]})
    return outcome


async def build_supplement_patch(source: str, original: SourceSemanticPlan, current: UserFacingTripResult,
                                 rows: list[Any], pipeline: TripUnderstandingPipeline) -> SupplementPatch:
    validation = validate_supplement(source, original, current, rows)
    result = current.model_copy(deep=True)
    additions = [mention for item in validation.accepted if isinstance(item.operation, AddSourceVisit) for mention in item.mentions]
    # Existing visits and their identities never go back through place search.
    resolved = await pipeline.run(source, prepared_plan=original.model_copy(update={
        "mentions": additions, "diagnostics": [], "unprocessed_count": 0, "unprocessed_by_day": {},
    })) if additions else None
    new_cards = {card.source_occurrence_id: card for day in resolved.public_result.days for card in day.activities} if resolved else {}
    new_choices = {card.source_occurrence_id: card for day in resolved.public_result.days for card in day.alternatives} if resolved else {}
    changed = set()
    applied = []
    after_cursor = {}
    detail_additions = {}
    routes_changed = False

    def reject(item: AcceptedSupplement, reason: str) -> None:
        validation.rejected.append({"operation_id": item.operation.operation_id, "reason": reason})

    for item in validation.accepted:
        op = item.operation
        if isinstance(op, SuggestSourceCorrection):
            suggestion = SourceCorrectionSuggestionView(
                suggestion_id=uuid5(NAMESPACE_URL, f"{original.source_hash}:{op.target_visit_id}:{op.source_quote}").hex,
                target_visit_id=op.target_visit_id, source_name=op.source_quote)
            if any(old.suggestion_id == suggestion.suggestion_id for old in result.correction_suggestions):
                reject(item, "ALREADY_EXTRACTED")
            elif len(result.correction_suggestions) >= MAX_TRIP_ACTIVITIES:
                reject(item, "CAPACITY_EXCEEDED")
            else:
                result.correction_suggestions.append(suggestion)
                applied.append(item)
            continue
        if isinstance(op, AddSourceDetail):
            day, parent = next((day, card) for day in result.days for card in [*day.activities, *day.alternatives]
                if (getattr(card, "visit_id", None) or card.alternative_id) == op.parent_visit_id)
            parent_id = item.mentions[0].parent_mention_id
            old = sorted([*(mention for mention in original.mentions if mention.parent_mention_id == parent_id),
                *detail_additions.get(parent_id, [])], key=_source_detail_order)
            old_views = [view for mention in old if (view := _public_source_detail(mention)) is not None]
            if parent.source_details != old_views:
                reject(item, "DETAIL_TARGET_CHANGED")
                continue
            combined = sorted([*old, *item.mentions], key=_source_detail_order)
            parent.source_details = [view for mention in combined if (view := _public_source_detail(mention)) is not None]
            detail_additions.setdefault(parent_id, []).extend(item.mentions)
            changed.add(day.label)
            applied.append(item)
            continue
        root = next(mention for mention in item.mentions if not mention.parent_mention_id)
        identity = source_occurrence_id(source, root)
        projected = (new_cards if op.operation == "ADD_VISIT" else new_choices).get(identity)
        if projected is None:
            reject(item, "ADDITION_NOT_PROJECTED")
            continue
        if root.replaces_mention_id:
            original_target = next((mention for mention in original.mentions if mention.mention_id == root.replaces_mention_id), None)
            occurrence = source_occurrence_id(source, original_target) if original_target else None
            target = next((card for day in result.days for card in day.activities if card.source_occurrence_id == occurrence), None) if occurrence else None
            if target is None:
                reject(item, "TARGET_CHANGED")
                continue
            projected = projected.model_copy(update={"replaces_visit_id": target.visit_id, "replaces_name": target.name,
                "replacement_condition": root.replacement_condition})
        while len(result.days) < root.day_index:
            result.days.append(TripDayView(label=f"Day {len(result.days) + 1}", activities=[]))
        day = result.days[root.day_index - 1]
        if op.operation == "ADD_ALTERNATIVE":
            # Patch-only projection positions exclude the existing route.
            # Require the user to pick its position when they later adopt it.
            day.alternatives.append(projected.model_copy(update={"insertion_position": None,
                "after_activity_token": None, "before_activity_token": None}))
        else:
            if sum(len(day.activities) for day in result.days) + len(result.lodging_constraints) >= MAX_TRIP_ACTIVITIES:
                reject(item, "CAPACITY_EXCEEDED")
                continue
            cursor = after_cursor.get((op.after_visit_id, op.before_visit_id), op.after_visit_id)
            position = next((i + 1 for i, card in enumerate(day.activities) if card.visit_id == cursor), len(day.activities))
            if cursor is None and op.before_visit_id:
                position = next(i for i, card in enumerate(day.activities) if card.visit_id == op.before_visit_id)
            day.activities.insert(position, projected.model_copy(deep=True))
            after_cursor[(op.after_visit_id, op.before_visit_id)] = projected.visit_id
            routes_changed = True
        applied.append(item)
        changed.add(day.label)
    validation.accepted = applied
    from app.trip_understanding.commands import _result_status, refresh_result_coverage
    if applied:
        if result.status != "LIMITED":
            result.status = _result_status(result.days, result.lodging_constraints)
        refresh_result_coverage(result)
    return SupplementPatch(result, validation, resolved, sorted(changed), routes_changed)
