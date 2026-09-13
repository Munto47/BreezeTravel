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

from app.trip_understanding.models import ActivityRole, StrictModel


FORMAT_INSTRUCTION = """
本请求只改变JSON表示，不改变上述旅行语义：用blocks替代activities。
blocks按原活动执行顺序排列，每块必须明确day_index（未知为null）、role、category、detail_mode。
块内items继承这些明确值；角色/日期/类别不同就开启下一块，不能按角色重新分组排序。
name必须明确：具名项填原文地点名；其定位引文就是name时不重复quote，原引文不同则填quote。
匿名项必须name:null并填写quote；不同原文名不能合并；再访保留独立项和正确occurrence。
detail_mode=EMPTY是明确本块各项没有内部安排；有安排用ITEMS并逐项填source_details；
无法判断用UNASSESSED，不得用EMPTY掩盖遗漏。详情的原父访问、日序和条件仍按原规则验证。
choice_groups/order_groups的activity_indices引用blocks依次展平后的items下标，不是块下标。
修复仍返回完整同格式JSON；不得修改原先正确访问。所有原文上下文仍须考虑。
"""


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
    return schema


def expand_compact_payload(payload: object) -> dict:
    value = json.loads(payload) if isinstance(payload, str) else payload
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


def compact_draft_payload(draft) -> dict:
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
