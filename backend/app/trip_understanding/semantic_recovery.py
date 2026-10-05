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


def _ticket_reference_table(source: str, start: int, end: int) -> bool:
    """A contiguous, explicitly labelled ticket table is not another visit.

    Every preceding row and the whole current row must contain only ticket
    metadata. A new heading, narrative, condition or visit ends this scope.
    This does not waive a separate occurrence of the same name in a route.
    """
    line_start = source.rfind('\n', 0, start) + 1
    line_end = source.find('\n', end)
    line_end = len(source) if line_end < 0 else line_end
    headings = list(re.finditer(r'(?m)^[ \t]*(?:景点)?(?:预约信息|门票信息|门票及预约信息|门票预约信息)[ \t]*[:：]?[ \t]*$', source[:line_start]))
    if not headings:
        return False
    body = source[headings[-1].end():line_end].strip('\r\n')
    clock = r'\d{1,2}[:：]\d{2}'
    clause = (r'(?:免费|无需预约|免预约|需预约|需要预约|需提前预约|提前[一二两三四五六七八九十\d]+天预约|'
              r'(?:门票|联票|票价|成人票|儿童票|学生票)?\s*\d+(?:\.\d+)?元(?:起|/人)?|'
              r'全天开放|' + clock + r'放票|' + clock + r'\s*[-—至]\s*' + clock + r'(?:开放)?)')
    for line in body.splitlines():
        match = re.fullmatch(r'\s*[^：:，,。；;！？!?\n]{1,40}[：:]\s*(' + clause + r'(?:[，,、]\s*' + clause + r')*)[。.]?\s*', line)
        if match is None:
            return False
    # Match the name column, never a word that happened to occur in metadata.
    colon = re.search(r'[:：]', source[line_start:line_end])
    return bool(body and colon and not source[line_start:start].strip()
                and not source[end:line_start + colon.start()].strip())


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
    if _ticket_reference_table(source, start, end):
        return 'TICKET_METADATA_REFERENCE'
    address_suffix = r'(?:(?:里|内)?的(?:这一个|这个|这一家|这家|这一)?|(?:这一个|这个|这一家|这家|这一))(?:馆|分馆|店)'
    if (re.search(r'(?:选(?:的是)?|位于|地址[：:为])[^，,。；;]{0,20}$', before)
            and (re.match(address_suffix + r'(?:[，,。；;]|$)', after)
                 or re.search(address_suffix + r'$', source[start:end]) and re.match(r'(?:[，,。；;]|$)', after))):
        return 'VENUE_ADDRESS_REFERENCE'
    # Local grammar must describe the noun, not introduce the next visit.
    if (re.search(r"(?:俯瞰|眺望|远眺|遥望)\s*$", before)
            and re.match(r"(?:的)?全景(?:[，,]|$)", after)
            and not re.search(r"前往|抵达|游览|参观|进入|入内|再去|再到|走到", after)):
        return "VIEWED_OBJECT"
    if (re.match(r"过马路即到(?:[，,]|$)", after)
            and (not before or before.endswith(("，", ",", "：", ":")))):
        return "DIRECTION_ORIGIN"
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


def _broken_name_slot(source: str, item: SemanticActivity, original: SemanticDraft,
                      rows: list[SemanticActivity]) -> int | None:
    """Keep a uniquely quoted atomic visit's slot when only its name is broken."""
    if not item.place_name:
        return None
    # A whole sentence or shared list is not an identity anchor for one member.
    quoted_span = _identity(source, item.model_copy(update={"place_name": None}))
    if quoted_span is None or quoted_span != _identity(source, item):
        return None

    def same_quote(row):
        return (row.source_quote, row.occurrence) == (item.source_quote, item.occurrence)

    slots = [index for index, row in enumerate(rows) if same_quote(row)]
    if len(slots) != 1 or sum(same_quote(row) for row in original.activities) != 1:
        return None
    candidate = rows[slots[0]]
    if (not candidate.place_name or _identity(source, candidate) is not None
            or any(getattr(candidate, key) != getattr(item, key)
                   for key in ("day_index", "role", "parent_source_quote"))):
        return None
    return slots[0]


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


def _summary_title_lists_name(title: str, name: str) -> bool:
    """Require a name in an outline, rather than treating Markdown as intent."""
    # Scheduled prose in a heading can itself be the first visit. Neither a
    # heading marker nor a vertical separator turns that visit into a summary.
    if re.search(r"早晨|清晨|早上|上午|中午|午后|下午|傍晚|晚上|夜间|凌晨|早晚|\d{1,2}[:：]\d{2}"
                 r"|游览|参观|到达|抵达|返回|离开", title):
        return False
    if re.search(r"[｜|]", title):
        outline = re.split(r"[｜|]", title, maxsplit=1)[1]
    else:
        marked = re.fullmatch(r"\s*[:：]?\s*(?:摘要|概览|行程摘要|路线|路线概览)\s*[:：]\s*(.+)", title)
        if marked is None:
            return False
        outline = marked[1]
    # A topic such as “中轴线：” may introduce a route list. The particular
    # name still has to be a complete list item, with at most a qualifier.
    outline = re.split(r"[:：]", outline, maxsplit=1)[-1]
    items = re.split(r"[—–→+、]", outline)
    return any(re.fullmatch(re.escape(name) + r"(?:[（(][^（）()\r\n]*[)）])?", item.strip())
               for item in items)


def _precedes_same_day_revisit(source: str, first: SemanticActivity, second: SemanticActivity) -> bool:
    """Choose an insertion slot from two explicit local periods, never sort a plan."""
    from app.trip_understanding.experience_inference import _unambiguous_literal_place_day

    if (not first.place_name or first.role.value != "PLANNED"
            or any(getattr(first, key) != getattr(second, key) for key in ("place_name", "day_index", "role"))
            or re.search(r"更正|取消|改到|改为|改成|对调|交换|顺延|移至|移到|挪|调整|原计划|最初计划", source)
            or _unambiguous_literal_place_day(source, first.place_name) != first.day_index):
        return False
    first_span, second_span = _identity(source, first), _identity(source, second)
    if first_span is None or second_span is None or first_span[1] > second_span[0]:
        return False
    periods = (("清晨", "早晨", "早上", "上午"), ("中午",), ("下午", "午后"), ("傍晚",), ("晚上", "夜间"))

    def explicit_period(span):
        left = max(source.rfind(mark, 0, span[0]) for mark in "\n，,。！？；;") + 1
        right = min((pos for mark in "\n，,。！？；;" if (pos := source.find(mark, span[1])) >= 0), default=len(source))
        local = source[left:right]
        ranks = {rank for rank, labels in enumerate(periods) if any(label in local for label in labels)}
        return next(iter(ranks)) if len(ranks) == 1 else None

    first_period, second_period = explicit_period(first_span), explicit_period(second_span)
    return first_period is not None and second_period is not None and first_period < second_period


def _summary_body_pair(source: str, name: str, day: int | None,
                       first: tuple[int, int], second: tuple[int, int]) -> tuple[int, int] | None:
    """Recognize one dated summary and one literal body occurrence, never revisits."""
    from app.trip_understanding.experience_inference import (
        SourceAnchorIndex, _explicit_day_count, _unambiguous_literal_place_day,
    )

    if not day or first == second or re.search(
        r"更正|取消|改到|改为|改成|对调|交换|顺延|移至|移到|挪|调整|原计划|最初计划", source,
    ):
        return None
    anchors = SourceAnchorIndex(source)
    headings = list(re.finditer(
        r"^[ \t]*(?P<markdown>#{1,6}[ \t]*)?(?P<label>(?:Day|D)\s*\d{1,2}(?![A-Za-z0-9]|\s*[-–—~～至到]\s*\d)"
        r"|第\s*[一二两三四五六七八九十\d]{1,3}\s*天)(?P<title>[^\r\n]*)",
        anchors.visible, re.M | re.I,
    ))
    days = [_explicit_day_count(match["label"]) for match in headings]
    if days != sorted(set(days)) or day not in days:
        return None
    index = days.index(day)
    heading = headings[index]
    if not _summary_title_lists_name(heading["title"], name) or re.search(
        r"二选一|方案|备选|或|如果|若|再访|重访|再次|两次|两趟", heading["title"],
    ):
        return None
    left = anchors.indices[heading.start()]
    body_left = anchors.indices[heading.end() - 1] + 1
    right = anchors.indices[headings[index + 1].start()] if index + 1 < len(headings) else len(source)
    summary, body = sorted((first, second))
    if not (left <= summary[0] < summary[1] <= body_left <= body[0] < body[1] <= right):
        return None
    # The name may appear in another day's visit. Within this day, however,
    # more than one body occurrence is insufficient evidence to merge visits.
    body_occurrences = []
    for occurrence in range(1, 161):
        try:
            span = anchors.locate(name, occurrence)
        except ValueError:
            break
        if body_left <= span[0] < span[1] <= right:
            body_occurrences.append(span)
    if (body_occurrences != [body] or explicit_reference_context(source, *body)
            or re.search(r"昨日|明日", source[body_left:right])
            or _unambiguous_literal_place_day(source[left:right], name) != day):
        return None
    return body


def merge_preserved_activities(source: str, original: SemanticDraft,
                               validated: InferenceProposal, repaired: SemanticDraft) -> SemanticDraft:
    """A repair can add/fix failed items but cannot erase validated occurrences.

    Protect identity, day, role and order together. Keys are original source
    occurrences, so two visits or branches sharing a name remain independent.
    This does not trust a repaired source_quote just because its name matches.
    """
    valid_spans = {(m.span_start, m.span_end) for m in validated.mentions}
    validated_by_span = {(m.span_start, m.span_end): m for m in validated.mentions}
    preserved = [(identity, item) for item in original.activities
                 if (identity := _identity(source, item)) is not None and identity in valid_spans]
    # A source occurrence with conflicting roles is not a preservation anchor.
    counts: dict[tuple[int, int], int] = {}
    for identity, _item in preserved:
        counts[identity] = counts.get(identity, 0) + 1
    preserved = [(identity, item) for identity, item in preserved if counts[identity] == 1]
    # A repair can change a quote from a daily summary to the single actual
    # visit (or vice versa). Keep the body anchor and retain the summary as a
    # reference; counting both as visits would preserve a known adapter error.
    order_originals = (original, repaired)
    repaired_rows = list(repaired.activities)
    relocated: dict[tuple[int, int], tuple[int, int]] = {}
    references = []
    uncertain_revisits = []
    aligned = []
    for identity, item in preserved:
        key = (item.place_name, item.day_index, item.role)
        matches = [index for index, row in enumerate(repaired_rows)
                   if (row.place_name, row.day_index, row.role) == key]
        if (item.place_name and item.category in {"景点", "地点"}
                and not item.parent_source_quote and item.role.value in {"PLANNED", "OPTIONAL"} and len(matches) == 1
                and sum((row.place_name, row.day_index, row.role) == key for row in original.activities) == 1):
            candidate = repaired_rows[matches[0]]
            candidate_span = _identity(source, candidate)
            body = _summary_body_pair(source, item.place_name, item.day_index, identity, candidate_span) if candidate_span else None
            if body is not None:
                clause_start = max(source.rfind(mark, 0, body[0]) for mark in "\n，,。！？；;") + 1
                before = source[clause_start:body[0]].rstrip(" *_")
                if re.search(r"(?:(?:再次|重新|再)(?:去|到|游览|参观|逛)?|返回|回到)\s*$", before):
                    # The outline does not prove when the earlier visit occurs.
                    # Preserve both source occurrences and expose uncertainty;
                    # “only one body noun” is not proof of only one visit.
                    uncertain_revisits.append(source[clause_start:body[1]])
                    body = None
            if body is not None and body != identity:
                protected = validated_by_span[identity]
                # A real time bound to the heading must not be stripped later
                # just because the name now quotes a different occurrence.
                if any(getattr(protected, field) is not None for field in (
                        "start_time", "end_time", "visit_duration_minutes", "role_evidence")):
                    body = None
                elif protected.city_hint:
                    from app.trip_understanding.experience_inference import SourceAnchorIndex, _validated_city

                    shifted_spans = [body if span == identity else span for span in valid_spans]
                    city, _evidence, removed = _validated_city(source, SourceAnchorIndex(source), item,
                                                              *body, shifted_spans)
                    if removed or city != protected.city_hint:
                        body = None
            if body is not None:
                body_item, summary_item = (item, candidate) if identity == body else (candidate, item)
                quote = {"source_quote": body_item.source_quote, "occurrence": body_item.occurrence}
                repaired_rows[matches[0]] = candidate.model_copy(update=quote)
                references.append(type(item)(source_quote=summary_item.source_quote,
                    occurrence=summary_item.occurrence, place_name=item.place_name,
                    role="REFERENCE", day_index=item.day_index, category=item.category))
                relocated[identity] = body
                item, identity = item.model_copy(update=quote), body
        aligned.append((identity, item))
    preserved = aligned
    repaired = repaired.model_copy(update={"activities": repaired_rows})
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
    invalid_time_spans = {relocated.get(span, span) for span in invalid_time_spans}
    invalid_city_spans = {relocated.get(span, span) for span in invalid_city_spans}
    improved = []
    for identity, item in preserved:
        candidates = repaired_by_identity.get(identity, [])
        if len(candidates) == 1:
            candidate = candidates[0]
            item = _fill_missing_lodging_evidence(source, item, candidate)
            if all(getattr(item, key) == getattr(candidate, key) for key in ("place_name", "day_index", "role")):
                from app.trip_understanding.inline_source_details import merge_inline_details

                # This exact parent occurrence may gain separately validated
                # details even when an unrelated repaired row remains invalid.
                item = item.model_copy(update={"source_details": merge_inline_details(
                    item.source_details, candidate.source_details)})
                if item.conditional_replacement is None and candidate.conditional_replacement is not None:
                    item = item.model_copy(update={"conditional_replacement": candidate.conditional_replacement})
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
        # The second answer may keep the exact atomic occurrence but invent a
        # different name. Preserve the verified original at that same slot,
        # rather than moving it past newly recovered visits. No repaired fact
        # is copied; the whole merged draft still passes the normal validator.
        broken_name_slot = _broken_name_slot(source, item, original, rows)
        if broken_name_slot is not None:
            rows[broken_name_slot] = item
            existing.add(identity)
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
        preceding = {key for key, _row in preserved[:position]}
        after_preserved = max((index + 1 for index, row in enumerate(rows)
                               if _identity(source, row) in preceding), default=0)
        # A missing morning visit belongs before a separately extracted evening
        # revisit. This only chooses that item's slot and cannot reorder the
        # already protected first answer or a corrected/cross-day itinerary.
        insert_at = min(insert_at, next((index for index, row in enumerate(rows)
            if index >= after_preserved and _precedes_same_day_revisit(source, item, row)), len(rows)))
        rows.insert(insert_at, item)
        existing.add(identity)
    # Preserve the relative order of all validated occurrences even when the
    # second answer reorders them. Newly repaired items keep the model's slots.
    protected_slots = [index for index, row in enumerate(rows) if _identity(source, row) in originals]
    if len(protected_slots) == len(preserved):
        for index, (_identity_key, item) in zip(protected_slots, preserved, strict=True):
            rows[index] = item
    reference_spans = {_identity(source, item) for item in rows if item.role.value == "REFERENCE"}
    rows.extend(item for item in references if _identity(source, item) not in reference_spans)
    if len(rows) > 160:
        # No truncation masquerades as a successful repair.
        return original
    unprocessed = list(repaired.unprocessed_quotes)
    for quote in uncertain_revisits:
        if quote not in unprocessed and len(unprocessed) < 80:
            unprocessed.append(quote)
    # At the existing 80-quote limit the plan is already explicitly partial;
    # retain every original warning rather than replacing one to add this one.
    from app.trip_understanding.choice_groups import remap_choice_groups

    merged = repaired.model_copy(update={"activities": rows, "unprocessed_quotes": unprocessed})
    # Recovery may insert a preserved row; reply indices must not move a group
    # onto a different visit. Rebind only by exact original source occurrence.
    merged = remap_choice_groups(source, repaired if repaired.choice_groups else original, merged)
    from app.trip_understanding.source_order import remap_semantic_order_groups

    return remap_semantic_order_groups(source, order_originals, merged)


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
