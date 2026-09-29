"""Conservative coverage of several explicitly separated source visits.

This consumes validated occurrences and a small literal connective grammar.
It never creates visits, assigns parents, or treats a paragraph quote as proof
that every noun in that paragraph was retained.
"""
import re

from app.trip_understanding.models import ActivityRole, SourceSemanticPlan


_DAY = re.compile(r"^\s*(?:Day\s*(\d{1,2})|第([一二三四五六七八九十\d]{1,3})天)\s*[：:]?", re.I)
_CLOCK_WORD = r"(?:(?:清晨|早上|上午|中午|下午|傍晚|晚上|最后|随后)\s*)?"
_ARRIVAL = re.compile(_CLOCK_WORD + r"(?:先去|先到|然后去|再去|再到|前往|去|到|返回|回到|回|再访)\s*")
_DETAIL_GLUE = re.compile(r"园内|馆内|寺内|院内|内部|重点|必看|必逛|必玩|入口|出口|进门|出门|"
    r"按顺序|先后|依次|然后|之后|随后|最后|参观|游览|体验|乘坐|游玩|"
    r"进入|出去|离开|入园|进园|出园|入馆|进馆|出馆|入内|先|再|看|去|到|进|出|从|由|经|在|为|是")
_PUNCTUATION = re.compile(r"[\s、：:（）()\[\]【】“”‘’\"'→—\-·*#]+")


def multiple_visit_fragments_covered(source: str, proposal: SourceSemanticPlan) -> bool:
    from app.trip_understanding.source_summary import explicit_source_relations
    from app.trip_understanding.semantic_supplement import _VISIT_CUE

    if "\x00" in source or any(d.category == "SOURCE_VISIT_UNRESOLVED" for d in proposal.diagnostics):
        return False
    proposal = explicit_source_relations(source, proposal)
    roots = sorted((m for m in proposal.mentions if not m.parent_mention_id and m.atomic_place_name), key=lambda m: m.span_start)
    details = [m for m in proposal.mentions if m.parent_mention_id and m.relation_type == "INTERNAL_DETAIL"]
    if len(roots) < 2 or not details:
        return False
    left = source.rfind("\n", 0, roots[0].span_start) + 1
    if _VISIT_CUE.search(source[:left]):
        return False
    # Earlier omitted visits are not a harmless document title. Only a
    # literal destination title may precede the first accounted-for visit.
    prefix = source[:left].strip()
    title = re.escape(proposal.destination_name) + r"(?:[一二两三四五六七八九十\d]+(?:天|日)(?:游)?)?[。！!\s]*"
    if prefix and not re.fullmatch(title, prefix):
        return False
    rows = sorted([*roots, *details], key=lambda m: m.span_start)
    if any(source[m.span_start:m.span_end] != m.raw_text for m in rows):
        return False
    if any(a.span_end > b.span_start for a, b in zip(rows, rows[1:])):
        return False
    tokens = {m.mention_id: f"\x00{i}\x00" for i, m in enumerate(rows)}
    by_token = {tokens[m.mention_id]: m for m in rows}
    by_id = {m.mention_id: m for m in roots}
    pieces, cursor = [], left
    for m in rows:
        if m.span_start < left:
            return False
        pieces.extend((source[cursor:m.span_start], tokens[m.mention_id]))
        cursor = m.span_end
    pieces.append(source[cursor:])
    text = "".join(pieces)
    current = None
    current_day = roots[0].day_index
    first_in_day = True
    for line in text.splitlines():
        heading = _DAY.match(line)
        if heading:
            raw_day = heading[1] or heading[2]
            # Unsupported Chinese composites remain unfinished, never guessed.
            current_day = int(raw_day) if raw_day.isdecimal() else {"一": 1, "二": 2, "三": 3, "四": 4,
                "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}.get(raw_day)
            if current_day is None:
                return False
            line = line[heading.end():]
            current, first_in_day = None, True
        for sentence in re.split(r"[。！？!?；;]", line):
            if not sentence.strip():
                continue
            # Only a previously source-validated literal replacement may
            # consume its repeated reference to the default visit's name.
            replacement = False
            for m in roots:
                original = by_id.get(m.replaces_mention_id)
                if not original or not original.atomic_place_name or not m.replacement_condition:
                    continue
                pattern = (r"\s*" + re.escape(m.replacement_condition) + r"[，,]\s*(?:就)?(?:把|将)\s*"
                    + re.escape(original.atomic_place_name) + r"\s*(?:替换成|换成|替换为|换为)\s*"
                    + re.escape(tokens[m.mention_id]) + r"(?:[，,]\s*(?:二者只选一个|两者只选一个|二选一))?\s*")
                if m.day_index == current_day and re.fullmatch(pattern, sentence):
                    replacement = True
                    break
            if replacement:
                continue
            for clause in re.split(r"[，,]", sentence):
                if not clause.strip():
                    continue
                present = [by_token[token] for token in re.findall(r"\x00\d+\x00", clause)]
                independent = [m for m in present if not m.parent_mention_id]
                if len(independent) > 1:
                    return False
                remainder = clause
                if independent:
                    root = independent[0]
                    prefix, suffix = clause.split(tokens[root.mention_id], 1)
                    if root.day_index != current_day:
                        return False
                    optional = root.role == ActivityRole.OPTIONAL and re.fullmatch(r"\s*(?:只是|仅作|作为)?备选\s*", suffix)
                    excluded = root.role == ActivityRole.EXCLUDED and re.fullmatch(r"\s*(?:这次|本次)?不去\s*", suffix)
                    arrival = root.role == ActivityRole.PLANNED and (_ARRIVAL.fullmatch(prefix.strip())
                        or (first_in_day and not _PUNCTUATION.sub("", prefix)))
                    if not (arrival or ((optional or excluded) and not prefix.strip())):
                        return False
                    current, first_in_day = root, False
                    if optional or excluded:
                        continue
                    remainder = suffix
                children = [m for m in present if m.parent_mention_id]
                if any(current is None or m.parent_mention_id != current.mention_id for m in children):
                    return False
                if _VISIT_CUE.search(clause) and not any(current and m.parent_mention_id == current.mention_id for m in details):
                    return False
                for child in children:
                    remainder = remainder.replace(tokens[child.mention_id], "")
                # Purpose restrictions are consumed only for the very visit
                # whose validated child establishes that purpose.
                purposes = {m.detail_kind for m in details if current and m.parent_mention_id == current.mention_id}
                if "PICKUP_ONLY" in purposes:
                    remainder = re.sub(r"(?:这次|本次)?不再进馆参观|(?:这次|本次)?不进馆参观|仅取物|只取物|门口", "", remainder)
                if "EXTERIOR_ONLY" in purposes:
                    remainder = re.sub(r"只看外观|不进馆|不进入|只在门外", "", remainder)
                remainder = _PUNCTUATION.sub("", _DETAIL_GLUE.sub("", remainder))
                if remainder:
                    return False
    return True
