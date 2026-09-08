"""Bounded recovery of source-validated semantic work, with no provider calls.

All source positions stay private. No name, date, role or POI is invented here.
The normal semantic/source validators remain the authority after a merge.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.trip_understanding.experience_inference import SemanticActivity, SemanticDraft
    from app.trip_understanding.models import InferenceProposal


def explicit_reference_context(source: str, start: int, end: int) -> str | None:
    """Identify narrow non-visit uses of a known noun, without assigning visits.

    These are source grammar cues, independent of the city/name dictionary.
    A repeated name by itself is never sufficient to waive coverage.
    """
    left = max(source.rfind(mark, 0, start) for mark in "\n。！？；;") + 1
    right = min((pos for mark in "\n。！？；;" if (pos := source.find(mark, end)) >= 0), default=len(source))
    before = source[left:start].replace("**", "").strip()
    after = source[end:right].replace("**", "").strip()
    sentence = before + source[start:end] + after
    # Local grammar must describe the noun, not introduce the next visit.
    if re.match(r"(?:面积|占地|始建于|建于|建成于|位于|地处)", after):
        return "DESCRIPTION"
    if re.match(r"住宿(?:推荐|建议)", sentence) and re.search(r"(?:附近|片区|区域)", after):
        return "LODGING_AREA_REFERENCE"
    if re.search(r"(?:在|距离|离)$", before) and re.match(r"(?:的)?(?:附近|周边)", after):
        return "LOCATION_REFERENCE"
    if (re.match(r"(?:需要注意的是|温馨提示|注意事项|预约提醒)[，,:：]?", sentence)
            and re.search(r"预约|预订|门票", sentence)
            and not re.search(r"前往|抵达|游览|参观|再去|再到|走到", sentence)):
        return "ADVISORY"
    if re.search(r"(?:我们将|今天将)聚焦于", before) and re.match(r"(?:与|及|和)周边区域", after):
        return "DAY_OVERVIEW"
    return None


def _identity(source: str, item: SemanticActivity) -> tuple[int, int] | None:
    from app.trip_understanding.experience_inference import SourceAnchorIndex

    anchors = SourceAnchorIndex(source)
    try:
        start, end = anchors.locate(item.source_quote, item.occurrence)
    except ValueError:
        return None
    if item.place_name:
        relative = anchors.place_span(start, end, item.place_name)
        if relative is None:
            return None
        return start + relative[0], start + relative[1]
    return start, end


def _fill_missing_lodging_evidence(source: str, original: SemanticActivity,
                                  repaired: SemanticActivity) -> SemanticActivity:
    from app.trip_understanding.experience_inference import (
        SourceAnchorIndex, _bound_lodging_evidence, _lodging_context_bounds,
    )

    if original.lodging_evidence or not original.lodging_event or not repaired.lodging_evidence:
        return original
    protected = ("place_name", "day_index", "role", "category", "lodging_event", "lodging_scope")
    if any(getattr(original, field) != getattr(repaired, field) for field in protected):
        return original
    # Keep the first activity's identity and meaning. Only the missing quote
    # can be copied, and it must cover this exact occurrence in its local day.
    candidate = original.model_copy(update={"lodging_evidence": repaired.lodging_evidence})
    anchors = SourceAnchorIndex(source)
    start, end = anchors.locate(original.source_quote, original.occurrence)
    span = _bound_lodging_evidence(anchors, candidate, start, end)
    left, right = _lodging_context_bounds(source, start, end)
    return candidate if span is not None and left <= span[0] < span[1] <= right else original


def merge_preserved_activities(source: str, original: SemanticDraft,
                               validated: InferenceProposal, repaired: SemanticDraft) -> SemanticDraft:
    """A repair can add/fix failed items but cannot erase validated occurrences.

    Protect identity, day, role and order together. Keys are original source
    occurrences, so two visits or branches sharing a name remain independent.
    This does not trust a repaired source_quote just because its name matches.
    """
    valid_spans = {(m.span_start, m.span_end) for m in validated.mentions}
    preserved = [(identity, item) for item in original.activities
                 if (identity := _identity(source, item)) is not None and identity in valid_spans]
    # A source occurrence with conflicting roles is not a preservation anchor.
    counts: dict[tuple[int, int], int] = {}
    for identity, _item in preserved:
        counts[identity] = counts.get(identity, 0) + 1
    preserved = [(identity, item) for identity, item in preserved if counts[identity] == 1]
    repaired_by_identity: dict[tuple[int, int], list[SemanticActivity]] = {}
    for item in repaired.activities:
        if (identity := _identity(source, item)) is not None:
            repaired_by_identity.setdefault(identity, []).append(item)
    # A valid place span does not certify the original row's rejected time.
    # Only an independently source-validated answer may improve those fields;
    # the merged answer is still fully validated by the caller afterward.
    from app.trip_understanding.experience_inference import SourceAnchorIndex, _proposal_from_live_draft

    try:
        _proposal_from_live_draft(source, repaired)
    except ValueError:
        repair_valid = False
    else:
        repair_valid = True
    # Partial name recovery can remove earlier rows, so diagnostic field
    # indices need not index the original draft. Its exact source occurrence
    # alone authorizes replacing failed timing or a failed city/evidence pair.
    invalid_time_quotes = {(issue.span_start, issue.span_end)
                           for issue in getattr(validated, "diagnostics", [])
                           if issue.category in {"TIME_EVIDENCE_NOT_IN_SOURCE", "COMMITMENT_EVIDENCE_NOT_IN_SOURCE"}
                           and issue.span_start is not None and issue.span_end is not None}
    invalid_time_spans = set()
    if invalid_time_quotes:
        anchors = SourceAnchorIndex(source)
        quoted_identities: dict[tuple[int, int], set[tuple[int, int]]] = {}
        for original_item in original.activities:
            identity = _identity(source, original_item)
            if identity is None:
                continue
            quote = anchors.locate(original_item.source_quote, original_item.occurrence)
            quoted_identities.setdefault(quote, set()).add(identity)
        # Timing diagnostics refer to the whole source_quote, which may include
        # prose around a name. Shared quotes containing several names do not
        # authorize changing either activity by a broad containment guess.
        invalid_time_spans = {next(iter(identities)) for quote, identities in quoted_identities.items()
                              if quote in invalid_time_quotes and len(identities) == 1}
    invalid_city_spans = {(issue.span_start, issue.span_end)
                          for issue in getattr(validated, "diagnostics", [])
                          if issue.category == "UNSUPPORTED_CITY_REMOVED"
                          and issue.span_start is not None and issue.span_end is not None}
    improved = []
    for identity, item in preserved:
        candidates = repaired_by_identity.get(identity, [])
        if len(candidates) == 1:
            candidate = candidates[0]
            item = _fill_missing_lodging_evidence(source, item, candidate)
            if repair_valid and all(getattr(item, key) == getattr(candidate, key)
                                    for key in ("place_name", "day_index", "role")):
                updates = {}
                if identity in invalid_time_spans:
                    updates.update({key: getattr(candidate, key) for key in (
                        "start_time", "end_time", "visit_duration_minutes", "timing_source",
                        "locked", "fixed_commitment", "time_evidence")})
                if item.category == "地点":
                    updates["category"] = candidate.category
                if identity in invalid_city_spans:
                    updates.update(city=candidate.city, city_evidence=candidate.city_evidence)
                item = item.model_copy(update=updates)
        improved.append((identity, item))
    preserved = improved
    originals = dict(preserved)
    rows = list(repaired.activities)
    for index, item in enumerate(rows):
        identity = _identity(source, item)
        if identity in originals:
            rows[index] = originals[identity]
    existing = {_identity(source, item) for item in rows}
    for position, (identity, item) in enumerate(preserved):
        if identity in existing:
            continue
        # A unique broken quote can still occupy the repaired item's intended
        # slot. Restore the already validated original quote/occurrence there;
        # never use this name-only fallback for same-day repeated visits.
        key = (item.place_name, item.day_index, item.role)
        def same_key(row):
            return (row.place_name, row.day_index, row.role) == key
        matching_slots = [index for index, row in enumerate(rows) if same_key(row)]
        if (item.place_name and len(matching_slots) == 1
                and sum(same_key(row) for row in original.activities) == 1
                and _identity(source, rows[matching_slots[0]]) is None):
            rows[matching_slots[0]] = item
            existing.add(identity)
            continue
        following = {key for key, _row in preserved[position + 1:]}
        insert_at = next((index for index, row in enumerate(rows) if _identity(source, row) in following), len(rows))
        rows.insert(insert_at, item)
        existing.add(identity)
    # Preserve the relative order of all validated occurrences even when the
    # second answer reorders them. Newly repaired items keep the model's slots.
    protected_slots = [index for index, row in enumerate(rows) if _identity(source, row) in originals]
    if len(protected_slots) == len(preserved):
        for index, (_identity_key, item) in zip(protected_slots, preserved, strict=True):
            rows[index] = item
    if len(rows) > 160:
        # No truncation masquerades as a successful repair.
        return original
    return repaired.model_copy(update={"activities": rows})


def improves_only_lodging_evidence(before: InferenceProposal, after: InferenceProposal) -> bool:
    """Keep a repaired quote even when an unrelated source error still remains."""
    fields = {"lodging_event", "lodging_scope", "lodging_role_uncertain", "lodging_evidence",
              "lodging_evidence_start", "lodging_evidence_end"}
    if after.unprocessed_count >= before.unprocessed_count:
        return False
    if [m.model_dump(exclude=fields) for m in before.mentions] != [m.model_dump(exclude=fields) for m in after.mentions]:
        return False
    # Discarding a failed source item must not look like better recovery.
    if [d for d in before.diagnostics if d.category != "LODGING_EVIDENCE_SCOPE_MISMATCH"] != [
        d for d in after.diagnostics if d.category != "LODGING_EVIDENCE_SCOPE_MISMATCH"]:
        return False
    return all(not current.lodging_role_uncertain or previous.lodging_role_uncertain
               for previous, current in zip(before.mentions, after.mentions, strict=True))


def complete_activities_from_truncated_json(content: str) -> dict | None:
    """Read only complete JSON objects in a top-level activities array.

    No regex extraction from model prose, dangling strings or nested objects.
    The caller must revalidate every object and report OUTPUT_TRUNCATED even
    when some usable rows survive. The original output is never logged.
    """
    decoder = json.JSONDecoder()
    position = 0

    def whitespace(index: int) -> int:
        while index < len(content) and content[index].isspace():
            index += 1
        return index

    position = whitespace(position)
    if position >= len(content) or content[position] != "{":
        return None
    position += 1
    result: dict = {}
    while position < len(content):
        try:
            key, position = decoder.raw_decode(content, whitespace(position))
        except ValueError:
            return None
        if not isinstance(key, str) or key in result:
            return None
        position = whitespace(position)
        if position >= len(content) or content[position] != ":":
            return None
        position = whitespace(position + 1)
        if key == "activities":
            if position >= len(content) or content[position] != "[":
                return None
            position += 1
            rows = []
            while len(rows) < 160:
                try:
                    row, end = decoder.raw_decode(content, whitespace(position))
                except ValueError:
                    break
                if not isinstance(row, dict):
                    break
                rows.append(row)
                position = whitespace(end)
                if position >= len(content) or content[position] != ",":
                    break
                position += 1
            if not rows:
                return None
            result["activities"] = rows
            return result
        try:
            value, position = decoder.raw_decode(content, position)
        except ValueError:
            return None
        if key not in {"destination", "day_labels", "unprocessed_quotes"}:
            return None
        result[key] = value
        position = whitespace(position)
        if position >= len(content) or content[position] != ",":
            return None
        position += 1
    return None
