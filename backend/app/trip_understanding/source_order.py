"""Source-order meaning and private visit lineage; no IO or regex inference.

Model assessments distinguish an initial route from required precedence. Literal
validation proves their source/visit binding, not the model's semantic accuracy.
Only the private, text-free state below belongs in immutable proposal_json.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Callable, Literal, Mapping, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from app.trip_understanding.models import ProposedMention
    from app.trip_understanding.relative_route_options import SourceOrderConstraints


class OrderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ActivityIndex = Annotated[int, Field(strict=True, ge=0, lt=160)]


class SemanticRequiredPrecedence(OrderModel):
    before_index: ActivityIndex
    after_index: ActivityIndex
    evidence: str = Field(min_length=1, max_length=5000)


class SemanticOrderGroup(OrderModel):
    """Wire indices refer to this answer's activities, never public tokens."""

    kind: Literal["INITIAL_ORDER", "REQUIRED_PRECEDENCE", "UNKNOWN"]
    activity_indices: tuple[ActivityIndex, ...] = Field(min_length=1, max_length=160)
    scope_quote: str | None = Field(default=None, max_length=5000)
    required_precedence: tuple[SemanticRequiredPrecedence, ...] = Field(default=(), max_length=160)


class RequiredPrecedenceDraft(OrderModel):
    before_mention_id: str
    after_mention_id: str
    evidence: str = Field(min_length=1, max_length=5000)


class SourceOrderGroupDraft(OrderModel):
    """For SourceSemanticPlan; wire activity indices must be mapped explicitly.

    INITIAL_ORDER is a positive semantic assessment that the listed ordering is
    not mandatory. Omission/UNKNOWN must never be converted to INITIAL_ORDER.
    The model reviews the whole source; scope_quote locates this particular group.
    """

    kind: Literal["INITIAL_ORDER", "REQUIRED_PRECEDENCE", "UNKNOWN"]
    member_mention_ids: tuple[str, ...] = Field(min_length=1, max_length=160)
    scope_quote: str | None = Field(default=None, max_length=50000)
    required_precedence: tuple[RequiredPrecedenceDraft, ...] = Field(default=(), max_length=160)

    @model_validator(mode="after")
    def consistent_kind(self):
        members = set(self.member_mention_ids)
        if len(members) != len(self.member_mention_ids):
            raise ValueError("Order group members must be distinct visits")
        if (self.kind == "REQUIRED_PRECEDENCE") != bool(self.required_precedence):
            raise ValueError("Only required precedence groups carry hard edges")
        for edge in self.required_precedence:
            if edge.before_mention_id == edge.after_mention_id or not {
                edge.before_mention_id, edge.after_mention_id,
            } <= members:
                raise ValueError("Order edges must connect distinct group members")
        return self


class BoundSourceOrderGroup(OrderModel):
    # Transient source-side contract. These offsets must not be persisted in
    # immutable revisions; seed_source_order_state strips every source field.
    kind: Literal["INITIAL_ORDER", "REQUIRED_PRECEDENCE"]
    member_mention_ids: tuple[str, ...]
    hard_precedence: tuple[tuple[str, str], ...]
    scope_start: int
    scope_end: int
    evidence_spans: tuple[tuple[int, int], ...] = ()


class SourceOrderAssessment(OrderModel):
    groups: tuple[BoundSourceOrderGroup, ...] = ()
    unknown_mention_ids: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()


def _unique_span(source: str, quote: str | None, left=0, right=None) -> tuple[int, int] | None:
    if not quote:
        return None
    right = len(source) if right is None else right
    start = source.find(quote, left, right)
    if start < 0 or source.find(quote, start + 1, right) >= 0:
        return None
    return start, start + len(quote)


def _eligible(mention: ProposedMention) -> bool:
    return (getattr(mention.role, "value", mention.role) == "PLANNED"
            and bool(mention.atomic_place_name)
            and mention.day_index is not None and mention.parent_mention_id is None)


def bind_source_order_groups(
    source: str, mentions: Sequence[ProposedMention], drafts: Sequence[SourceOrderGroupDraft],
) -> SourceOrderAssessment:
    """Accept only explicit, uniquely source-bound assessments; keep gaps unknown."""
    by_id = {m.mention_id: m for m in mentions}
    if len(by_id) != len(mentions):
        raise ValueError("Source mentions must have unique identities")
    eligible = {m.mention_id for m in mentions if _eligible(m)}
    counts = Counter(mid for draft in drafts for mid in draft.member_mention_ids)
    groups = []
    issues = []
    for draft in drafts:
        members = [by_id[mid] for mid in draft.member_mention_ids if mid in by_id]
        if draft.kind == "UNKNOWN":
            continue
        if (len(members) != len(draft.member_mention_ids)
                or any(not _eligible(m) or counts[m.mention_id] != 1 for m in members)
                or len({m.day_index for m in members}) != 1):
            issues.append("ORDER_MEMBERS_UNBOUND")
            continue
        scope = _unique_span(source, draft.scope_quote)
        if scope is None or any(
            not scope[0] <= m.span_start < m.span_end <= scope[1]
            or source[m.span_start:m.span_end] != m.raw_text for m in members
        ):
            issues.append("ORDER_SCOPE_UNBOUND")
            continue
        hard = []
        evidence_spans = []
        valid = True
        for edge in draft.required_precedence:
            span = _unique_span(source, edge.evidence, *scope)
            a, b = by_id[edge.before_mention_id], by_id[edge.after_mention_id]
            if span is None or any(not span[0] <= m.span_start < m.span_end <= span[1] for m in (a, b)):
                valid = False
                break
            hard.append((a.mention_id, b.mention_id))
            evidence_spans.append(span)
        # Cycles contradict an executable precedence assessment. Do not break a
        # cycle by dropping an edge or using the original mention order as gold.
        remaining = set(draft.member_mention_ids)
        while valid and remaining:
            roots = {mid for mid in remaining if not any(b == mid and a in remaining for a, b in hard)}
            if not roots:
                valid = False
            remaining -= roots
        if not valid:
            issues.append("ORDER_PRECEDENCE_UNBOUND")
            continue
        groups.append(BoundSourceOrderGroup(
            kind=draft.kind, member_mention_ids=draft.member_mention_ids,
            hard_precedence=tuple(dict.fromkeys(hard)), scope_start=scope[0], scope_end=scope[1],
            evidence_spans=tuple(evidence_spans),
        ))
    verified = {mid for group in groups for mid in group.member_mention_ids}
    return SourceOrderAssessment(groups=tuple(groups), unknown_mention_ids=tuple(
        m.mention_id for m in mentions if m.mention_id in eligible - verified
    ), issues=tuple(issues))


def bind_semantic_order_groups(source, original, mentions) -> SourceOrderAssessment:
    """Map original wire rows through source spans after adapter expansion.

    A combined original row can supply multiple validated visits to a group,
    but cannot be guessed as one endpoint of a hard edge. Repeated names in a
    different source occurrence are never an identity fallback.
    """
    from app.trip_understanding.semantic_recovery import _identity

    mapped = {}
    for index, row in enumerate(original.activities):
        span = _identity(source, row)
        mapped[index] = tuple(m.mention_id for m in mentions if span is not None
            and span[0] <= m.span_start < m.span_end <= span[1]
            and _eligible(m) and m.role == row.role
            and (row.day_index is None or row.day_index == m.day_index))
    drafts = []
    issues = []
    for group in original.order_groups:
        members = tuple(mid for index in group.activity_indices for mid in mapped.get(index, ()))
        try:
            if any(not mapped.get(index) for index in group.activity_indices):
                raise ValueError("Unbound original order member")
            edges = []
            for edge in group.required_precedence:
                if (len(mapped.get(edge.before_index, ())) != 1
                        or len(mapped.get(edge.after_index, ())) != 1
                        or edge.before_index not in group.activity_indices
                        or edge.after_index not in group.activity_indices):
                    raise ValueError("Ambiguous original precedence endpoint")
                edges.append(RequiredPrecedenceDraft(
                    before_mention_id=mapped[edge.before_index][0],
                    after_mention_id=mapped[edge.after_index][0], evidence=edge.evidence))
            drafts.append(SourceOrderGroupDraft(kind=group.kind, member_mention_ids=members,
                scope_quote=group.scope_quote, required_precedence=tuple(edges)))
        except ValueError:
            issues.append("ORDER_WIRE_MEMBERS_UNBOUND")
            # Keep valid members occupied by UNKNOWN so an overlapping free
            # group cannot erase this contradictory/unbound assessment.
            if members:
                drafts.append(SourceOrderGroupDraft(kind="UNKNOWN",
                    member_mention_ids=tuple(dict.fromkeys(members))))
    result = bind_source_order_groups(source, mentions, drafts)
    return result.model_copy(update={"issues": (*result.issues, *issues)})


def remap_semantic_order_groups(source, originals, updated):
    """Preserve order meaning when repair inserts/reorders answer rows.

    Missing repair metadata does not erase an earlier reviewed group. Distinct
    overlapping answers remain contradictory and are rejected by the binder.
    """
    from app.trip_understanding.semantic_recovery import _identity

    def key(row):
        span = _identity(source, row)
        return (*span, row.day_index, row.role) if span else None

    positions = {}
    for index, row in enumerate(updated.activities):
        identity = key(row)
        if identity is not None:
            positions.setdefault(identity, []).append(index)
    groups = []
    for original in originals:
        for group in original.order_groups:
            mapping = {}
            for index in group.activity_indices:
                identity = key(original.activities[index]) if index < len(original.activities) else None
                found = positions.get(identity, ())
                if len(found) == 1:
                    mapping[index] = found[0]
            if len(mapping) != len(group.activity_indices):
                # Remap the known remainder as UNKNOWN, never preserve stale
                # integer indices that might now point to another occurrence.
                if mapping:
                    candidate = SemanticOrderGroup(kind="UNKNOWN", activity_indices=tuple(mapping.values()))
                else:
                    continue
            elif any(edge.before_index not in mapping or edge.after_index not in mapping
                     for edge in group.required_precedence):
                candidate = SemanticOrderGroup(kind="UNKNOWN", activity_indices=tuple(mapping.values()))
            else:
                candidate = group.model_copy(update={
                    "activity_indices": tuple(mapping[index] for index in group.activity_indices),
                    "required_precedence": tuple(edge.model_copy(update={
                        "before_index": mapping[edge.before_index], "after_index": mapping[edge.after_index],
                    }) for edge in group.required_precedence),
                })
            if candidate not in groups:
                groups.append(candidate)
    return updated.model_copy(update={"order_groups": groups})


def retain_source_order_assessment(assessment, mentions) -> SourceOrderAssessment:
    """Only complete groups still attached to actual root visits survive."""
    eligible = {m.mention_id: m for m in mentions if _eligible(m)}
    groups = tuple(group for group in assessment.groups
        if all(mid in eligible and group.scope_start <= eligible[mid].span_start
               < eligible[mid].span_end <= group.scope_end for mid in group.member_mention_ids)
        and len({eligible[mid].day_index for mid in group.member_mention_ids}) == 1)
    known = {mid for group in groups for mid in group.member_mention_ids}
    return SourceOrderAssessment(groups=groups,
        unknown_mention_ids=tuple(mid for mid in eligible if mid not in known), issues=assessment.issues)


def remap_source_order_assessment(assessment, ids, *, offset=0, source_start=0):
    """Move a day-local assessment to whole-source IDs and coordinates."""
    groups = []
    for group in assessment.groups:
        if (group.scope_start < source_start or not set(group.member_mention_ids) <= ids.keys()
                or any(left < source_start for left, _right in group.evidence_spans)):
            continue
        groups.append(group.model_copy(update={
            "member_mention_ids": tuple(ids[mid] for mid in group.member_mention_ids),
            "hard_precedence": tuple((ids[a], ids[b]) for a, b in group.hard_precedence),
            "scope_start": group.scope_start + offset, "scope_end": group.scope_end + offset,
            "evidence_spans": tuple((left + offset, right + offset) for left, right in group.evidence_spans),
        }))
    return SourceOrderAssessment(groups=tuple(groups), issues=assessment.issues)


class PrivateVisitOrigin(OrderModel):
    visit_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_kind: Literal["SOURCE_BOUND", "USER_ADDED", "UNKNOWN"]


class PrivateOrderGroup(OrderModel):
    group_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    member_visit_ids: tuple[str, ...]
    hard_precedence: tuple[tuple[str, str], ...] = ()
    origin: Literal["SOURCE", "USER_ADDED", "USER_OVERRIDE"] = "SOURCE"


class PrivateSourceOrderState(OrderModel):
    version: Literal[1] = 1
    # Keys are current public tokens, never the anonymous secret capability.
    bindings: dict[str, PrivateVisitOrigin] = Field(default_factory=dict)
    groups: tuple[PrivateOrderGroup, ...] = ()

    @model_validator(mode="after")
    def valid_references(self):
        ids = {v.visit_id for v in self.bindings.values()}
        members = [mid for g in self.groups for mid in g.member_visit_ids]
        if len(ids) != len(self.bindings) or len(members) != len(set(members)):
            raise ValueError("Visit lineage and reviewed groups must be unambiguous")
        if len({g.group_id for g in self.groups}) != len(self.groups):
            raise ValueError("Order groups must be distinct")
        for g in self.groups:
            if not g.member_visit_ids or not set(g.member_visit_ids) <= ids:
                raise ValueError("Order group contains an unavailable visit")
            if any(a == b or not {a, b} <= set(g.member_visit_ids) for a, b in g.hard_precedence):
                raise ValueError("Order edge contains an unavailable visit")
        if any(v.source_kind == "UNKNOWN" and v.visit_id in members for v in self.bindings.values()):
            raise ValueError("Unknown source visits cannot be marked reviewed")
        return self


@dataclass(frozen=True)
class VisitLocation:
    activity_token: str
    day_index: int
    position: int


def _locations(locations: Sequence[VisitLocation]) -> dict[str, tuple[int, int]]:
    result = {v.activity_token: (v.day_index, v.position) for v in locations}
    if (len(result) != len(locations) or len(set(result.values())) != len(locations)
            or any(not v.activity_token or not 1 <= v.day_index <= 14 or v.position < 0 for v in locations)):
        raise ValueError("Visit locations must be unique authoritative positions")
    return result


def _new_id() -> str:
    return uuid4().hex


def load_source_order_state(
    value: object, locations: Sequence[VisitLocation], *, id_factory: Callable[[], str] = _new_id,
) -> PrivateSourceOrderState:
    """Legacy missing state is UNKNOWN, not reconstructed from names or order."""
    tokens = _locations(locations)
    if value is None:
        return PrivateSourceOrderState(bindings={token: PrivateVisitOrigin(
            visit_id=id_factory(), source_kind="UNKNOWN",
        ) for token in tokens})
    state = PrivateSourceOrderState.model_validate(value)
    if set(state.bindings) != set(tokens):
        raise ValueError("Stored visit lineage does not match this revision")
    return state


def seed_source_order_state(
    assessment: SourceOrderAssessment, mention_to_token: Mapping[str, str],
    locations: Sequence[VisitLocation], *, id_factory: Callable[[], str] = _new_id,
) -> PrivateSourceOrderState:
    """Use the compiler's exact mention→token map, never name/POI matching."""
    tokens = _locations(locations)
    if len(set(mention_to_token.values())) != len(mention_to_token):
        raise ValueError("Compiled source visits must map one-to-one")
    source_tokens = set(mention_to_token.values())
    bindings = {token: PrivateVisitOrigin(visit_id=id_factory(), source_kind=(
        "SOURCE_BOUND" if token in source_tokens else "UNKNOWN"
    )) for token in tokens}
    groups = []
    for group in assessment.groups:
        if any(mention_to_token.get(mid) not in bindings for mid in group.member_mention_ids):
            continue
        ids = {mid: bindings[mention_to_token[mid]].visit_id for mid in group.member_mention_ids}
        groups.append(PrivateOrderGroup(
            group_id=id_factory(), member_visit_ids=tuple(ids[mid] for mid in group.member_mention_ids),
            hard_precedence=tuple((ids[a], ids[b]) for a, b in group.hard_precedence),
        ))
    return PrivateSourceOrderState(bindings=bindings, groups=tuple(groups))


def _violations(groups, positions):
    return {(g.group_id, a, b) for g in groups for a, b in g.hard_precedence
            if positions[a] >= positions[b]}


def _remaining_precedence(group, members):
    """Contract deleted visits, stopping at each surviving visit.

    A→B→C still requires A→C after B is removed. Do not add shortcuts
    through surviving visits: their existing edges remain independently subject
    to an explicit manual move. Walk each deleted node once per start so shared
    paths cannot duplicate edges or make a malformed cycle loop indefinitely.
    """
    if members == group.member_visit_ids:
        return group.hard_precedence
    remaining = set(members)
    successors = {}
    for a, b in group.hard_precedence:
        successors.setdefault(a, []).append(b)
    edges = []
    for start in members:
        pending = list(reversed(successors.get(start, ())))
        visited = set()
        while pending:
            target = pending.pop()
            if target in remaining:
                if target == start:
                    raise ValueError("Deleted visit path contains cyclic source order")
                edges.append((start, target))
            elif target not in visited:
                visited.add(target)
                pending.extend(reversed(successors.get(target, ())))
    return tuple(dict.fromkeys(edges))


def advance_source_order_state(
    previous: PrivateSourceOrderState,
    before: Sequence[VisitLocation], after: Sequence[VisitLocation], token_map: Mapping[str, str],
    *, user_moved_token: str | None = None, replaced_tokens: frozenset[str] = frozenset(),
    user_added_tokens: frozenset[str] = frozenset(), id_factory: Callable[[], str] = _new_id,
) -> PrivateSourceOrderState:
    """Inherit one business snapshot; restore uses the TARGET revision as previous.

    Pass user_moved_token only for an actual manual ACTIVITY_MOVE (no route preview
    credential). Identity-changing replace/rename/confirmation belongs in
    replaced_tokens. Unmapped new tokens remain UNKNOWN unless the caller marks
    an explicit user insertion. No operation guesses a source relation by name.
    """
    old_positions, new_positions = _locations(before), _locations(after)
    if set(previous.bindings) != set(old_positions) or not replaced_tokens <= set(old_positions):
        raise ValueError("Source snapshot differs from command input")
    mapped = {old: token_map.get(old, old) for old in old_positions}
    if len(set(mapped.values())) != len(mapped):
        raise ValueError("Command token mapping is not one-to-one")
    reverse = {new: old for old, new in mapped.items() if new in new_positions}
    if not user_added_tokens <= set(new_positions) or user_added_tokens & set(reverse):
        raise ValueError("User additions must identify genuinely new visits")
    bindings = {}
    invalidated = {previous.bindings[token].visit_id for token in replaced_tokens}
    for token in new_positions:
        old = reverse.get(token)
        if old is not None and old not in replaced_tokens:
            bindings[token] = previous.bindings[old]
        else:
            bindings[token] = PrivateVisitOrigin(visit_id=id_factory(), source_kind=(
                "USER_ADDED" if token in user_added_tokens else "UNKNOWN"
            ))
    ids = {v.visit_id for v in bindings.values()}
    positions = {v.visit_id: new_positions[token] for token, v in bindings.items()}
    old_by_id = {v.visit_id: old_positions[token] for token, v in previous.bindings.items()}
    groups = []
    for g in previous.groups:
        # Replacing a place invalidates its whole reviewed source scope. Deleting
        # an intermediate visit must retain its surviving neighbours' precedence.
        if invalidated & set(g.member_visit_ids):
            continue
        members = tuple(mid for mid in g.member_visit_ids if mid in ids)
        if members:
            groups.append(g.model_copy(update={"member_visit_ids": members,
                "hard_precedence": _remaining_precedence(g, members)}))
    if user_moved_token is not None:
        if (user_moved_token not in previous.bindings or ids != set(old_by_id)
                or replaced_tokens or user_added_tokens):
            raise ValueError("Manual move must preserve the existing visits")
        moved = previous.bindings[user_moved_token].visit_id
        others_before = sorted((v for v in old_by_id if v != moved), key=old_by_id.get)
        others_after = sorted((v for v in positions if v != moved), key=positions.get)
        if others_before != others_after or any(old_by_id[v][0] != positions[v][0] for v in others_before):
            raise ValueError("Manual move cannot authorize unrelated visit changes")
        updated = []
        for g in groups:
            hard = []
            overridden = False
            for a, b in g.hard_precedence:
                if positions[a] >= positions[b] and moved in (a, b) and old_by_id[a] < old_by_id[b]:
                    a, b = b, a
                    overridden = True
                hard.append((a, b))
            updated.append(g.model_copy(update={"hard_precedence": tuple(hard),
                "origin": "USER_OVERRIDE" if overridden else g.origin}))
        groups = updated
    if _violations(groups, positions) - _violations(groups, old_by_id):
        raise ValueError("Automatic edit violates an explicit source order")
    for token in user_added_tokens:
        groups.append(PrivateOrderGroup(group_id=id_factory(), member_visit_ids=(bindings[token].visit_id,),
                                        origin="USER_ADDED"))
    return PrivateSourceOrderState(bindings=bindings, groups=tuple(groups))


def route_order_constraints(state: PrivateSourceOrderState) -> SourceOrderConstraints:
    # Local import keeps this type module safe for future SemanticDraft/plan fields.
    from app.trip_understanding.relative_route_options import SourceOrderConstraints
    return SourceOrderConstraints(
        verified_visit_ids=frozenset(mid for g in state.groups for mid in g.member_visit_ids),
        hard_precedence=tuple(edge for g in state.groups for edge in g.hard_precedence),
    )
