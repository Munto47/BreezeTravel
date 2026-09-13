"""Development-only wire compression; all meaning still uses the live validator.

Blocks are consecutive runs, not buckets. Flattening never sorts visits or
changes the indices used by choices and source-order assessments.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from typing import Literal

from pydantic import Field, create_model

from app.trip_understanding.models import ActivityRole, SemanticDiagnostic, SourceSemanticPlan, StrictModel
from app.trip_understanding.source_order import SemanticOrderGroup


FORMAT_INSTRUCTION = """
本请求只改变JSON表示，不改变上述旅行语义：用blocks替代activities。
blocks按原活动执行顺序排列，每块必须明确day_index（未知为null）、role、category、detail_mode。
块内items继承这些明确值；角色/日期/类别不同就开启下一块，不能按角色重新分组排序。
name必须明确：具名项填原文地点名；其定位引文就是name时不重复quote，原引文不同则填quote。
匿名项必须name:null并填写quote；不同原文名不能合并；再访保留独立项和正确occurrence。
detail_mode=EMPTY是明确本块各项没有内部安排；有安排用ITEMS并逐项填source_details；
无法判断用UNASSESSED，不得用EMPTY掩盖遗漏。详情的原父访问、日序和条件仍按原规则验证。
choice_groups/order_groups的activity_indices引用blocks依次展平后的items下标，不是块下标。
order_groups不重复整段scope_quote，改用scope_range:{start:{quote,occurrence},end:{quote,occurrence}}。
两端quote必须是本次原文逐字引文，occurrence为它在整份本次原文从头计数的正整数出现次数；
范围从start引文开头到end引文结尾。不能改写空白/Markdown、借另一日同名位置或猜范围。
这只定位原有顺序证据；kind仍须依据全文明确评估，不能从相邻端点推断INITIAL_ORDER。
required_precedence的完整evidence及choice_groups证据保持原格式，不得省略或压缩。
修复仍返回完整同格式JSON；不得修改原先正确访问。所有原文上下文仍须考虑。
"""


class CompactScopeAnchor(StrictModel):
    quote: str = Field(strict=True, min_length=1, max_length=120)
    occurrence: int = Field(strict=True, ge=1, le=50000)


class CompactOrderScopeRange(StrictModel):
    start: CompactScopeAnchor
    end: CompactScopeAnchor


class CompactSemanticOrderGroup(SemanticOrderGroup):
    # Local compatibility accepts the old complete scope_quote. The active
    # compact request schema advertises only the required range representation.
    scope_range: CompactOrderScopeRange | None = None


def _literal_spans(source: str, quote: str) -> list[tuple[int, int]]:
    found, start = [], 0
    while quote:
        start = source.find(quote, start)
        if start < 0:
            break
        found.append((start, start + len(quote)))
        start += 1
    return found


def expand_order_scope(source: str, value: object) -> str:
    scope = CompactOrderScopeRange.model_validate(value)
    positions = []
    for anchor in (scope.start, scope.end):
        spans = _literal_spans(source, anchor.quote)
        if anchor.occurrence > len(spans):
            raise ValueError("COMPACT_SCOPE_ANCHOR_NOT_FOUND")
        positions.append(spans[anchor.occurrence - 1])
    (left, start_end), (end_start, right) = positions
    if left > end_start or start_end > right:
        raise ValueError("COMPACT_SCOPE_ENDPOINTS_REVERSED")
    quote = source[left:right]
    if not 1 <= len(quote) <= 5000 or len(_literal_spans(source, quote)) != 1:
        raise ValueError("COMPACT_SCOPE_NOT_UNIQUE_OR_TOO_LONG")
    return quote


def compact_order_scope(source: str, quote: str) -> dict:
    spans = _literal_spans(source, quote)
    if len(spans) != 1:
        raise ValueError("COMPACT_SCOPE_NOT_UNIQUE")
    left, right = spans[0]
    prefix, suffix = quote[:24], quote[-24:]
    value = dict(start=dict(quote=prefix,
        occurrence=_literal_spans(source, prefix).index((left, left + len(prefix))) + 1),
        end=dict(quote=suffix,
        occurrence=_literal_spans(source, suffix).index((right - len(suffix), right)) + 1))
    if expand_order_scope(source, value) != quote:
        raise ValueError("COMPACT_SCOPE_NOT_REVERSIBLE")
    return value


def _expand_order_group(raw: object, source: str | None, activity_count: int) -> tuple[dict, bool]:
    try:
        group = CompactSemanticOrderGroup.model_validate(raw)
        value = group.model_dump(mode="json", exclude_unset=True, exclude={"scope_range"})
        if "scope_range" not in group.model_fields_set:
            return value, False
        if source is None or group.scope_range is None:
            raise ValueError("COMPACT_SCOPE_SOURCE_REQUIRED")
        quote = expand_order_scope(source, group.scope_range)
        if group.scope_quote is not None and group.scope_quote != quote:
            raise ValueError("COMPACT_SCOPE_REPRESENTATIONS_CONFLICT")
        return {**value, "scope_quote": quote}, False
    except ValueError:
        if isinstance(raw, dict):
            try:
                # Bad endpoint evidence does not erase a well-typed hard
                # constraint. It remains unbound until the same constraints
                # have source evidence; a later free group cannot replace it.
                base = SemanticOrderGroup.model_validate({key: val for key, val in raw.items()
                    if key not in {"scope_range", "scope_quote"}})
                return {**base.model_dump(mode="json", exclude_unset=True), "scope_quote": None}, True
            except ValueError:
                pass
        # An invalid range never discards valid visits or gives an overlapping
        # free group permission. An unbound UNKNOWN occupies the supplied valid
        # indices; without trustworthy membership, keep all visits unassessed.
        indices = raw.get("activity_indices") if isinstance(raw, dict) else None
        if not (isinstance(indices, (list, tuple)) and 1 <= len(indices) <= 160
                and all(type(i) is int and 0 <= i < 160 for i in indices)):
            indices = list(range(min(activity_count, 160)))
        return dict(kind="UNKNOWN", activity_indices=list(indices), scope_quote=None), True


@lru_cache(maxsize=1)
def _wire_model():
    # Lazy import avoids a cycle with the provider. Clone the existing fields
    # so optional meal/lodging/identity/detail capabilities cannot drift.
    from app.trip_understanding.experience_inference import SemanticActivity, SemanticDraft

    inherited = {name: (field.annotation, deepcopy(field))
        for name, field in SemanticActivity.model_fields.items()
        if name not in {"source_quote", "place_name", "day_index", "role", "category"}}
    item = create_model("CompactSemanticItem", __base__=StrictModel,
        name=(str | None, Field(max_length=40)),
        quote=(str | None, Field(default=None, min_length=1, max_length=1000)), **inherited)
    block = create_model("CompactSemanticBlock", __base__=StrictModel,
        day_index=(int | None, Field(ge=1, le=14)),
        role=(ActivityRole, ...),
        category=(SemanticActivity.model_fields["category"].annotation, ...),
        detail_mode=(Literal["EMPTY", "ITEMS", "UNASSESSED"], ...),
        # The wire advertises the bound. Locally flatten an oversized reply
        # first, so the existing source-grounded capacity check can distinguish
        # actual 161 visits from repeated model rows instead of retrying to fit.
        items=(list[item], Field(min_length=1, json_schema_extra={"maxItems": 160})))
    top = {name: (field.annotation, deepcopy(field))
        for name, field in SemanticDraft.model_fields.items() if name != "activities"}
    top["order_groups"] = (list[CompactSemanticOrderGroup], deepcopy(SemanticDraft.model_fields["order_groups"]))
    return create_model("CompactSemanticDraft", __base__=StrictModel,
        blocks=(list[block], Field(max_length=160)), **top)


def compact_schema(active_legacy_schema: dict) -> dict:
    schema = _wire_model().model_json_schema()
    schema["required"] = ["blocks", *[name for name in active_legacy_schema["required"] if name != "activities"]]
    legacy_props = active_legacy_schema["$defs"]["SemanticActivity"]["properties"]
    item_props = schema["$defs"]["CompactSemanticItem"]["properties"]
    for name in list(item_props):
        if name not in {"name", "quote"} and name not in legacy_props:
            del item_props[name]
    schema["properties"]["day_labels"] = deepcopy(active_legacy_schema["properties"]["day_labels"])
    group = schema["$defs"]["CompactSemanticOrderGroup"]
    del group["properties"]["scope_quote"]
    group["properties"]["scope_range"] = {"$ref": "#/$defs/CompactOrderScopeRange"}
    group["required"] = [*group["required"], "scope_range"]
    return schema


def expand_compact_payload(payload: object, *, source: str | None = None) -> dict:
    value = json.loads(payload) if isinstance(payload, str) else payload
    if isinstance(value, dict) and isinstance(value.get("order_groups"), list):
        blocks = value.get("blocks") if isinstance(value.get("blocks"), list) else []
        count = sum(len(b.get("items", [])) for b in blocks
            if isinstance(b, dict) and isinstance(b.get("items"), list))
        groups = [_expand_order_group(group, source, count) for group in value["order_groups"]]
        value = {**value, "order_groups": [group for group, _ in groups]}
    parsed = _wire_model().model_validate(value)
    result = parsed.model_dump(mode="json", exclude_unset=True)
    activities = []
    unprocessed = list(result.get("unprocessed_quotes", []))
    for block in result.pop("blocks"):
        for supplied in block["items"]:
            row = dict(supplied)
            name = row.pop("name")
            quote = row.pop("quote", None)
            if quote is None:
                if name is None:
                    raise ValueError("COMPACT_ANONYMOUS_QUOTE_REQUIRED")
                quote = name
            if block["detail_mode"] == "EMPTY":
                if row.get("source_details"):
                    raise ValueError("COMPACT_CONTRADICTORY_DETAILS")
                row["source_details"] = []
            elif block["detail_mode"] == "UNASSESSED" and "source_details" in row:
                raise ValueError("COMPACT_CONTRADICTORY_DETAILS")
            if "source_details" not in row and quote not in unprocessed:
                # Historical readers permit absent details. The compact wire's
                # explicit unknown decision must remain unfinished even when
                # there is no lexical cue; use the existing source-fragment
                # contract and its original 80-fragment bound, never trim it.
                unprocessed.append(quote)
            row.update(source_quote=quote, place_name=name, day_index=block["day_index"],
                role=block["role"], category=block["category"])
            activities.append(row)
    result["activities"] = activities
    if unprocessed or "unprocessed_quotes" in result:
        result["unprocessed_quotes"] = unprocessed
    return result


def mark_unbound_compact_order(proposal: SourceSemanticPlan) -> SourceSemanticPlan:
    # Only failed bindings count here. A valid explicit UNKNOWN or ordinary
    # missing assessment does not claim a missing place. No source span is
    # marked covered: unrelated known-place omissions must remain visible.
    failures = {"ORDER_SCOPE_UNBOUND", "ORDER_MEMBERS_UNBOUND",
        "ORDER_WIRE_MEMBERS_UNBOUND", "ORDER_PRECEDENCE_UNBOUND"}
    category = "ORDER_EVIDENCE_UNPROCESSED"
    if not failures.intersection(proposal.order_assessment.issues) or any(
        issue.category == category for issue in proposal.diagnostics
    ):
        return proposal
    return proposal.model_copy(update={
        "diagnostics": [*proposal.diagnostics, SemanticDiagnostic(category=category, field="order_groups")],
        "unprocessed_count": proposal.unprocessed_count + 1,
    })


def without_repaired_order_placeholders(source: str, original, repaired):
    """Replace only unbound placeholders, after independent source validation.

    The ordinary merge still protects every visit and every grounded order
    group. Replacement requires the same members, kind and hard evidence;
    retaining both copies would otherwise falsely create overlap.
    """
    from app.trip_understanding.experience_inference import _proposal_from_live_draft
    from app.trip_understanding.semantic_recovery import _identity

    try:
        checked = _proposal_from_live_draft(source, repaired)
    except ValueError:
        return original
    by_id = {m.mention_id: (m.span_start, m.span_end, m.day_index, m.role) for m in checked.mentions}
    bound = {(frozenset(by_id[mid] for mid in group.member_mention_ids), group.kind)
        for group in checked.order_assessment.groups}

    def keys(draft, indices):
        found = []
        for index in indices:
            if index >= len(draft.activities):
                return frozenset()
            row = draft.activities[index]
            span = _identity(source, row)
            if (span is None or row.role != ActivityRole.PLANNED or row.day_index is None
                    or not row.place_name or source[span[0]:span[1]] != row.place_name):
                return frozenset()
            found.append((*span, row.day_index, row.role))
        return frozenset(found) if len(set(found)) == len(indices) else frozenset()

    def edges(draft, group):
        found = []
        for edge in group.required_precedence:
            before, after = keys(draft, [edge.before_index]), keys(draft, [edge.after_index])
            if not before or not after:
                return None
            found.append((next(iter(before)), next(iter(after)), edge.evidence))
        return frozenset(found)

    replacements = []
    explicit_unknown = {by_id[mid] for mid in checked.order_assessment.explicit_unknown_mention_ids}
    for group in repaired.order_groups:
        members = keys(repaired, group.activity_indices)
        if members and (members, group.kind) in bound:
            replacements.append((members, group.kind, edges(repaired, group)))
        elif group.kind == "UNKNOWN" and members and members <= explicit_unknown:
            replacements.append((members, "UNKNOWN", frozenset()))
    kept = []
    for group in original.order_groups:
        placeholder = group.scope_quote is None
        members = keys(original, group.activity_indices) if placeholder else frozenset()
        original_edges = edges(original, group)
        same = bool(members) and any(members == new_members and group.kind == kind
            and original_edges is not None and original_edges == new_edges
            for new_members, kind, new_edges in replacements)
        if not same:
            kept.append(group)
    return original.model_copy(update={"order_groups": kept})


def compact_draft_payload(draft, *, source: str | None = None) -> dict:
    value = draft.model_dump(mode="json", exclude_unset=True)
    blocks = []
    for activity, raw in zip(draft.activities, value.pop("activities"), strict=True):
        row = dict(raw)
        # An absent required name cannot become an explicit anonymous choice.
        if "place_name" not in row:
            raise ValueError("COMPACT_NAME_DECISION_REQUIRED")
        name = row.pop("place_name")
        quote = row.pop("source_quote")
        mode = ("UNASSESSED" if "source_details" not in row else
            "ITEMS" if row["source_details"] else "EMPTY")
        header = {"day_index": activity.day_index, "role": activity.role.value,
            "category": activity.category, "detail_mode": mode}
        for field in ("day_index", "role", "category"):
            row.pop(field, None)
        if mode == "EMPTY":
            row.pop("source_details")
        # A default occurrence adds no information; explicit revisits do.
        if row.get("occurrence") == 1:
            row.pop("occurrence")
        row = {"name": name, **({"quote": quote} if quote != name else {}), **row}
        if not blocks or any(blocks[-1][key] != val for key, val in header.items()):
            blocks.append({**header, "items": []})
        blocks[-1]["items"].append(row)
    if source is not None:
        for group in value.get("order_groups", []):
            quote = group.get("scope_quote")
            if quote:
                try:
                    scope = compact_order_scope(source, quote)
                except ValueError:
                    # Keep the actual invalid old evidence in a repair example;
                    # never manufacture endpoints to make it fit the new wire.
                    continue
                group.pop("scope_quote")
                group["scope_range"] = scope
    return {**value, "blocks": blocks}


def complete_compact_items_from_truncated_json(content: str) -> dict | None:
    """Recover closed rows only after all their explicit block headers.

    Object key order is untrusted. An items-first incomplete block is not
    recoverable; prior complete blocks are. No day/role/details are guessed.
    """
    decoder = json.JSONDecoder()
    required = {"day_index", "role", "category", "detail_mode"}

    def skip(pos):
        while pos < len(content) and content[pos].isspace():
            pos += 1
        return pos

    def entries(pos):
        pos = skip(pos)
        if pos >= len(content) or content[pos] != "{":
            return
        pos += 1
        while True:
            pos = skip(pos)
            try:
                key, end = decoder.raw_decode(content, pos)
            except ValueError:
                return
            end = skip(end)
            if not isinstance(key, str) or end >= len(content) or content[end] != ":":
                return
            value_at = skip(end + 1)
            yield key, value_at
            try:
                _, pos = decoder.raw_decode(content, value_at)
            except ValueError:
                return
            pos = skip(pos)
            if pos >= len(content) or content[pos] != ",":
                return
            pos += 1

    result, blocks = {}, []
    for key, pos in entries(0):
        if key != "blocks":
            try:
                result[key], _ = decoder.raw_decode(content, pos)
            except ValueError:
                break
            continue
        if pos >= len(content) or content[pos] != "[":
            break
        pos = skip(pos + 1)
        while pos < len(content):
            try:
                block, end = decoder.raw_decode(content, pos)
            except ValueError:
                header, items = {}, []
                for field, start in entries(pos):
                    if field == "items":
                        if not required <= header.keys() or content[start:start + 1] != "[":
                            break
                        start = skip(start + 1)
                        while start < len(content):
                            try:
                                item, end = decoder.raw_decode(content, start)
                            except ValueError:
                                break
                            if not isinstance(item, dict):
                                break
                            items.append(item)
                            start = skip(end)
                            if content[start:start + 1] != ",":
                                break
                            start = skip(start + 1)
                        break
                    try:
                        header[field], _ = decoder.raw_decode(content, start)
                    except ValueError:
                        break
                if items:
                    blocks.append({**header, "items": items})
                break
            if not isinstance(block, dict):
                break
            blocks.append(block)
            pos = skip(end)
            if content[pos:pos + 1] != ",":
                break
            pos = skip(pos + 1)
        # A closed blocks array can precede destination/day/choice fields.
        # Let entries advance over that complete array and retain every later
        # complete value. If the array itself was cut, entries stops naturally;
        # it never scans past a partial value or fabricates the next field.
    return {**result, "blocks": blocks} if blocks else None
