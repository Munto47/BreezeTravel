"""Public example semantics, rebound to each submission before compilation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.trip_understanding.models import SourceSemanticPlan

DATA = Path(__file__).with_name("data")


def catalog():
    return json.loads((DATA / "public-examples.json").read_text(encoding="utf-8"))


def normalize(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def select_example(source, reference=None):
    if reference and not any(item["id"] == reference.get("id") and item["version"] == reference.get("version") for item in catalog()):
        return None
    for example in catalog():
        if normalize(source) == example["text"]:
            return {"id": example["id"], "version": example["version"], "mode": "exact"}
    if reference:
        selection = {**reference, "mode": "incremental"}
        try:
            if provisional_plan(source, selection) is not None:
                return selection
        except (ValueError, KeyError, OSError, StopIteration):
            pass
    return None


def seed_plan(example_id, version):
    example = next(item for item in catalog() if item["id"] == example_id and item["version"] == version)
    value = json.loads((DATA / f"{example_id}-semantics-v{version}.json").read_text(encoding="utf-8"))
    if value["version"] != version:
        raise ValueError("example semantics version mismatch")
    plan = SourceSemanticPlan.model_validate({**value["plan"],
        "source_hash": hashlib.sha256(example["text"].encode()).hexdigest(),
        "binding": {"_source_inventory": value["source_inventory"]}})
    validate_seed(example["text"], plan)
    return example, plan


def validate_seed(source, plan):
    from app.trip_understanding.source_inventory import inventory_covers
    from app.trip_understanding.pipeline import EvidenceCompiler
    if plan.unprocessed_count or not inventory_covers(source, plan, plan.binding["_source_inventory"]):
        raise ValueError("incomplete example semantics")
    if any(source[item.span_start:item.span_end] != item.raw_text for item in plan.mentions):
        raise ValueError("example source anchor mismatch")
    EvidenceCompiler().compile(source, plan)


def exact_plan(source, selection):
    example, plan = seed_plan(selection["id"], selection["version"])
    if normalize(source) != example["text"]:
        raise ValueError("example no longer matches source")
    # Map canonical character boundaries, including CRLF and leading whitespace.
    offsets = []
    start = len(source) - len(source.lstrip())
    position = start
    while position < len(source.rstrip()):
        offsets.append(position)
        position += 2 if source[position:position + 2] == "\r\n" else 1
    offsets.append(position)
    def rebind(value):
        if isinstance(value, list):
            return [rebind(item) for item in value]
        if isinstance(value, dict):
            return {key: offsets[item] if isinstance(item, int) and
                    (key.endswith("_start") or key.endswith("_end")) else rebind(item)
                    for key, item in value.items()}
        return value
    result = SourceSemanticPlan.model_validate({**rebind(plan.model_dump(mode="json")),
        "source_hash": hashlib.sha256(source.encode()).hexdigest(),
        "binding": {**plan.binding, "example_preprocessing": selection,
                    "external_calls": 0, "transport_calls": [], "stream_complete": True}})
    validate_seed(source, result)
    return result


def provisional_plan(source, selection):
    """Only whole unchanged day paragraphs can supply an unconfirmed baseline."""
    from app.trip_understanding.models import SemanticDiagnostic
    from app.trip_understanding.source_order import SourceOrderAssessment
    example, plan = seed_plan(selection["id"], selection["version"])
    original = example["text"]
    header = original.splitlines()[0]
    if len(source) > 500 or normalize(source).splitlines()[0] != header:
        return None
    # An added document-wide instruction invalidates every baseline paragraph.
    # With no scoped correspondence, use ordinary full-text understanding.
    for line in source.splitlines():
        if line and line not in original and not line.startswith("Day "):
            if any(word in line for word in ("全部", "所有", "全程", "整趟", "整个", "整篇", "目的地", "改去", "换到")):
                return None
    blocks = []
    for line in original.splitlines():
        if line.startswith("Day ") and source.count(line) == 1:
            blocks.append((original.index(line), original.index(line) + len(line), source.index(line)))
    def relocate(value, left, right, target):
        if isinstance(value, list):
            return [relocate(item, left, right, target) for item in value]
        if isinstance(value, dict):
            return {key: (item + target - left if left <= item <= right else None)
                    if isinstance(item, int) and (key.endswith("_start") or key.endswith("_end"))
                    else relocate(item, left, right, target) for key, item in value.items()}
        return value
    mentions = []
    for item in plan.mentions:
        block = next((b for b in blocks if b[0] <= item.span_start < item.span_end <= b[1]), None)
        if block:
            data = relocate(item.model_dump(), *block)
            data.update(semantic_review="PENDING", mention_id="baseline-" + item.mention_id)
            for field in ("parent_mention_id", "replaces_mention_id"):
                if data[field]:
                    data[field] = "baseline-" + data[field]
            mentions.append(type(item).model_validate(data))
    ids = {item.mention_id for item in mentions}
    mentions = [item for item in mentions if (not item.parent_mention_id or item.parent_mention_id in ids)
                and (not item.replaces_mention_id or item.replaces_mention_id in ids)]
    if not any(item.role == "PLANNED" and not item.parent_mention_id for item in mentions):
        return None
    return plan.model_copy(update={"source_hash": hashlib.sha256(source.encode()).hexdigest(),
        "mentions": mentions, "binding": {"example_preprocessing": selection, "stream_complete": False},
        "order_assessment": SourceOrderAssessment(), "unprocessed_count": 1,
        "diagnostics": [SemanticDiagnostic(category="STREAM_INCOMPLETE", field="source")]})


def merge_provisional(plan, provisional):
    if provisional is None:
        return plan
    keys = {(m.span_start, m.span_end, m.raw_text) for m in plan.mentions}
    remaining = [m for m in provisional.mentions if (m.span_start, m.span_end, m.raw_text) not in keys]
    ids = {m.mention_id for m in remaining}
    remaining = [m for m in remaining if not m.parent_mention_id or m.parent_mention_id in ids]
    mentions = sorted([*plan.mentions, *remaining], key=lambda m: (m.day_index or 1, m.span_start, m.sequence_index))
    return plan.model_copy(update={"mentions": [m.model_copy(update={"sequence_index": i}) for i, m in enumerate(mentions)],
        "day_count": max(plan.day_count, provisional.day_count), "unprocessed_count": max(1, plan.unprocessed_count)})


def incremental_baseline(selection):
    example, plan = seed_plan(selection["id"], selection["version"])
    return {"source": example["text"], "visits": [m.model_dump(mode="json", exclude_defaults=True)
            for m in plan.mentions], "order": plan.order_assessment.model_dump(mode="json")}


def validate_delta(payload, selection):
    _, base = seed_plan(selection["id"], selection["version"])
    roots = {m.mention_id: m for m in base.mentions if not m.parent_mention_id}
    accounted = set()
    for item in payload["activities"]:
        operation, identifier = item.get("operation"), item.get("base_visit_id")
        if operation == "ADD":
            if identifier is not None:
                raise ValueError("new visit cannot reuse a baseline identity")
            continue
        if identifier not in roots or identifier in accounted:
            raise ValueError("incremental visit identity mismatch")
        accounted.add(identifier)
        if operation == "KEEP":
            old = roots[identifier]
            if (item.get("place_name") != old.atomic_place_name or item["role"] != old.role
                    or item.get("day_index") != old.day_index or item.get("city") != old.city_hint):
                raise ValueError("retained visit changed meaning")
    for identifier in payload.get("deleted_base_ids", []):
        if identifier not in roots or identifier in accounted:
            raise ValueError("incremental deletion identity mismatch")
        accounted.add(identifier)
    if accounted != set(roots):
        raise ValueError("incremental response did not account for every baseline visit")
