"""Locate explicitly unselected guide alternatives without choosing places.

Ranges refer to the unchanged source string. This deliberately supports only
clear day/branch headings and explicitly labelled conditional quote blocks.
Comparisons, recommendations and incomplete alternative structures do nothing.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


_DAY_LABEL = r"(?:第\s*(?:\d{1,2}|[一二两三四五六七八九十]{1,3})\s*天|(?:Day|D)\s*\d{1,2}(?![\dA-Za-z]))"
_DAY_HEADING = re.compile(
    rf"^[ \t]*(?:>[ \t]*)?(?:#{{1,6}}[ \t]*)?(?P<label>{_DAY_LABEL})(?P<title>[^\r\n]*)",
    re.M | re.I,
)
_BRANCH_HEADING = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:方案|版本)[ \t]*"
    r"(?P<label>[ABabＡＢａｂ一二12])(?=[ \t:：|｜·—－\-（(]|$)[^\r\n]*",
    re.M,
)
_NEGATED_CHOICE = re.compile(
    r"(?:取消|撤销|不再是|不再|无需|无须|不用|不必|不是|并非|不)[ \t]*(?:这次)?[ \t]*二选一|"
    r"(?:不要|不用|无需|不必|禁止)[ \t]*(?:替换|换成)|"
    r"(?:不是|并非)[ \t]*(?:备选|替换方案)"
)
_SETTLED_CHOICE = re.compile(
    r"已(?:经)?[ \t]*(?:选定|选好|选择|选|采用|确定|决定)[ \t]*(?:了)?[ \t]*(?:方案|版本)?[ \t]*[ABabＡＢａｂ一二12]|"
    r"(?:方案|版本)[ \t]*[ABabＡＢａｂ一二12][ \t]*(?:不执行|不采用|不选择|作废)|"
    r"(?:最终|最后|已经|现已)[^。；;\n]{0,12}"
    r"(?:选定|选好|选择|采用|执行|确定|选)[ \t]*(?:了)?[ \t]*(?:方案|版本)?[ \t]*[ABabＡＢａｂ一二12]|"
    r"(?:方案|版本)[ \t]*[ABabＡＢａｂ一二12][ \t]*(?:已经|已)?[ \t]*(?:选定|确定|取消)|"
    r"二选一[^。；;\n]{0,12}(?:已选定|已确定|已经决定|已选好)|"
    r"(?:取消|撤销|不再)[ \t]*(?:这次)?[ \t]*二选一"
)
_RELATIVE_DAY = re.compile(r"次日|翌日|明天|后天|另一天|某天|择日|日期未定|日期待定|\d{1,2}[月/.-]\d{1,2}(?:日|号)?")
_COMMON_PLAN = re.compile(r"共同(?:安排|行程)|无论(?:选择|选)|不论(?:选择|选)|不管(?:选择|选)|两(?:个|种)?方案(?:都|均)")
_OPTIONAL_NAME = (
    r"[A-Za-z0-9\u4e00-\u9fff·][A-Za-z0-9\u4e00-\u9fff· \t]{0,22}?"
    r"(?:博物馆|博物院|美术馆|公园|古镇|咖啡馆|咖啡|会址|胡同|大街|斜街|屯|寺|塔)"
)
_OPTIONAL_AFTER = re.compile(
    rf"(?:可以去|可去|可顺路参观)[ \t]*(?:\*\*)?(?P<name>{_OPTIONAL_NAME})(?:\*\*)?"
    r"(?=$|[。！？；;，,、\n）)]|逛街|闲逛|散步|参观|游览|打卡|看展)"
)
_OPTIONAL_BEFORE = re.compile(
    rf"(?:^|[。！？；;，,、\n：:（(])[ \t]*(?:[-•][ \t]*)?(?:\*\*)?"
    rf"(?P<name>{_OPTIONAL_NAME})(?:\*\*)?[ \t]*(?:可以打卡|可顺路参观|可以参观)"
)
_OPTIONAL_UNSAFE_CONTEXT = re.compile(
    r"https?://|www\.|[\"“”‘’？?]|(?:引用|引文|转述|原文|资料|作者|据说|例如|示例)|"
    r"(?:不要|不能|不可|不去|不想|不打算|不考虑|不必|无需|别去|勿去|取消|禁止|并非|不是)"
)
_OPTIONAL_UNSAFE_NAME = re.compile(
    r"(?:如果|想|可以|可去|推荐|介绍|附近|旁边|当地|一家|这家|游客|适合|喝|吃|品尝|"
    r"必须|先去|先到|再去|再到|的|以及|或者|和|与|地址|路口)|\d+号|单元|\d+室"
)
_BEVERAGE_NAME = re.compile(r"美式|拿铁|摩卡|浓缩|手冲|冰滴|冷萃|特调|热|冰|特色|精品")
_WHOLE_DAY_REPLACEMENT = re.compile(
    rf"(?:把|将)[ \t]*{_DAY_LABEL}[ \t]*替换(?:为|成)?[ \t]*(?:\*\*)?"
    r"(?P<name>[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff·]{1,23}?)(?:\*\*)?"
    r"[ \t]*(?:一整天|全天)(?=$|[ \t。！？；;，,\n）)])",
    re.I,
)
_WHOLE_DAY_SETTLED = re.compile(
    r"(?:已(?:经)?|最终|最后)(?:决定|选定|确定|采用)|"
    r"(?:取消|撤销|不要)(?:上述|该|此)(?:替代|替换)方案"
)
_WHOLE_DAY_UNSAFE_NAME = re.compile(r"^(?:去|到|在)|参观|游览|游玩|打卡|活动|休息|散步|睡觉")
_MEAL_STREET_NAME = r"[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff·]{0,20}?(?:步行街|胡同|大街|街|路)"
_MEAL_STREET_CHOICE = re.compile(
    r"^[ \t]*(?:[-*•][ \t]+)?(?:中午|午餐|晚餐)[ \t]*[:：][ \t]*(?:\*\*)?"
    rf"(?P<first>{_MEAL_STREET_NAME})(?:\*\*)?[ \t]*[/／][ \t]*(?:\*\*)?"
    rf"(?P<second>{_MEAL_STREET_NAME})(?:\*\*)?[ \t]*(?:吃[^\r\n]+|用餐[^\r\n]*)\r?$",
    re.M,
)
_MEAL_STREET_UNSAFE = re.compile(
    r"依次|按顺序|分别|两个都|两处都|两条都|两条路都|两条街都|两者都|二者都|"
    r"都去|都逛|都吃|都要|都用餐|都安排|均(?:去|逛|吃|用餐)|各自|"
    r"路线|线路|路由|途经|经过|路过|穿过|导航|步行到|走到|沿着|换乘|转乘|"
    r"先去|再去|先到|再到|已选|已决定|已经选择|最终|更正"
)
_VISIT_STEM = r"[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff·]{1,18}"
_VISIT_END = r"(?=$|[ \t。！？；;，,、\r\n）)])"
# A meal heading and an exact street followed by a bounded eating clause are
# enough to retain that street. Cuisine names are open-ended; non-meal senses
# and completed/negative eating descriptions must not become visit labels.
_MEAL_ACTION = (
    r"(?:用餐|就餐|吃(?!(?:亏|惊|力|紧|苦|饱|撑|闭门羹|官司|回扣|罚单|老本|一堑|"
    r"了|过|完|不|得))[^\W\d_]{1,12})"
)
_VISIT_PATTERNS = (
    re.compile(r"^[ \t]*(?:[-*•][ \t]+)?(?:中午|午餐|晚餐|晚上)[ \t]*[:：][ \t]*(?:\*\*)?"
               r"(?!(?:前往|先去|再去|步行到|走到|沿着|就近|随便|某|在|从|到|去))"
               rf"(?P<name>{_MEAL_STREET_NAME})(?:\*\*)?[ \t]*(?P<meal_action>{_MEAL_ACTION}){_VISIT_END}", re.M),
    re.compile(rf"(?:^|[。！？；;，,\n：:])[ \t]*(?:[-•][ \t]*)?"
               rf"顺着[ \t]*(?:\*\*)?(?P<name>{_VISIT_STEM}路)(?:\*\*)?[ \t]*慢慢走{_VISIT_END}"),
    re.compile(rf"(?:^|[。！？；;，,\n：:])[ \t]*(?:[-•][ \t]*)?(?:\*\*)?"
               rf"(?P<name>{_VISIT_STEM}坊)(?:\*\*)?[ \t]*简单逛一圈(?:即可|就好)?{_VISIT_END}"),
    re.compile(rf"(?:^|[。！？；;，,\n：:）) \t])傍晚[ \t：:]*"
               rf"(?:\*\*)?(?P<name>{_VISIT_STEM}滩)(?:\*\*)?[ \t]*看落日{_VISIT_END}"),
)
_VISIT_SOFT_CONTEXT = re.compile(
    r"如果|假如|要是|只要|否则|时间充裕|有空|看情况|可以|可选|备选|可去|可顺路|"
    r"建议|推荐|二选一|介绍|示例|示范|曾经|去年|昨天|上次|回忆|"
    r"不(?:再)?(?:顺着|逛|看)|别|勿|取消"
)
_VISIT_GENERIC_NAME = re.compile(r"美丽|漂亮|开阔|宽阔|一条|一段|某条|某个|这条|这片|那里|河边|海边")


@dataclass(frozen=True)
class _Heading:
    start: int
    end: int
    day: int | None
    title: str


def _day_number(label: str) -> int | None:
    token = re.sub(r"\s+", "", label)
    number = re.sub(r"^(?:第|day|d)|天$", "", token, flags=re.I)
    digits = {char: value for value, char in enumerate("零一二三四五六七八九")}
    digits["两"] = 2
    if number.isdigit():
        day = int(number)
    elif number in digits:
        day = digits[number]
    elif number.count("十") == 1:
        left, right = number.split("十")
        if (left and left not in digits) or (right and right not in digits):
            return None
        day = digits.get(left, 1) * 10 + digits.get(right, 0)
    else:
        return None
    return day if 1 <= day <= 14 else None


def _scope_day(text: str, heading_day: int | None) -> int | None:
    if heading_day is None or _RELATIVE_DAY.search(text):
        return None
    references = {_day_number(match[0]) for match in re.finditer(_DAY_LABEL, text, re.I)}
    return heading_day if not references or references == {heading_day} else None


def _conditional_quote(first_line: str) -> bool:
    if _NEGATED_CHOICE.search(first_line):
        return False
    return bool(
        re.match(r"如果[^\n]+替换方案[ \t]*(?:[:：]|为|是|$)", first_line)
        or re.match(r"备选[ \t]*[:：][ \t]*如果[^\n]+(?:换成|替换当日|替换当天)", first_line)
    )


def choice_scopes(source: str) -> list[tuple[int, int, int | None]]:
    """Return explicit OPTIONAL ranges, with only a literal heading's day.

    A correction selecting/cancelling an option disables this narrow automatic
    classification; the semantic pass must handle that source. No place name
    is extracted and no default branch or inferred calendar day is supplied.
    """
    if _SETTLED_CHOICE.search(source):
        return []
    headings = [
        _Heading(match.start(), match.end(), _scope_day(match[0], _day_number(match["label"])), match["title"])
        for match in _DAY_HEADING.finditer(source)
    ]
    scopes: list[tuple[int, int, int | None]] = []
    for index, heading in enumerate(headings):
        if "二选一" not in heading.title or _NEGATED_CHOICE.search(heading.title):
            continue
        end = headings[index + 1].start if index + 1 < len(headings) else len(source)
        section = source[heading.end:end]
        branches = list(_BRANCH_HEADING.finditer(section))
        names = {unicodedata.normalize("NFKC", match["label"]).casefold() for match in branches}
        names = {"a" if name in {"a", "一", "1"} else "b" for name in names}
        if len(names) < 2 or _COMMON_PLAN.search(section):
            continue
        start = heading.end + branches[0].start()
        scopes.append((start, end, _scope_day(source[start:end], heading.day)))

    lines = source.splitlines(keepends=True)
    offset = 0
    line_index = 0
    while line_index < len(lines):
        line = lines[line_index]
        quoted = re.match(r"[ \t]*>[ \t]*(\S[^\r\n]*)", line)
        if not quoted:
            offset += len(line)
            line_index += 1
            continue
        start = offset
        first = quoted[1]
        while line_index < len(lines) and re.match(r"[ \t]*>[ \t]*\S", lines[line_index]):
            offset += len(lines[line_index])
            line_index += 1
        end = min((heading.start for heading in headings if start < heading.start < offset), default=offset)
        if not _conditional_quote(first) or any(left <= start and end <= right for left, right, _day in scopes):
            continue
        preceding = [heading for heading in headings if heading.start < start]
        day = preceding[-1].day if preceding else None
        scopes.append((start, end, _scope_day(source[start:end], day)))
    return sorted(scopes)


def explicit_binary_choice_clauses(source: str) -> list[tuple[int, int, int]]:
    """Bound two unselected clauses in a literal day heading, without NER.

    Only a flat ``二选一: clause, clause`` heading is supported. The caller
    must obtain names from semantics; these spans are not place labels.
    Decisions, quoted paragraphs and ambiguous day/branch structures do nothing.
    """
    if _SETTLED_CHOICE.search(source) or re.search(
        r"已(?:经)?(?:选择|选定|选好|确定|决定)|最终|更正|取消|撤销", source,
    ):
        return []
    clauses: list[tuple[int, int, int]] = []
    for heading in _DAY_HEADING.finditer(source):
        line, title = heading[0], heading["title"]
        day = _scope_day(line, _day_number(heading["label"]))
        if (
            day is None
            or line.lstrip().startswith(">")
            or _OPTIONAL_UNSAFE_CONTEXT.search(line)
            or _NEGATED_CHOICE.search(line)
            or re.search(r"(?:方案|版本)\s*[ABabＡＢａｂ一二12]|三选|3选", title)
            or re.match(r"\s*(?:[-–—~～/、]|至|到)\s*(?:第|Day|D)?\s*[\d一二两三四五六七八九十]", title, re.I)
            or re.search(r"(?:引用|引文|原文|示例|资料)[：:]\s*$", source[:heading.start()])
            or source[:heading.start()].count("```") % 2
            or re.search(r'["“‘]\s*$', source[:heading.start()])
        ):
            continue
        choices = list(re.finditer(r"二选一[ \t]*[:：]", title))
        if len(choices) != 1 or title.count("二选一") != 1:
            continue
        choice = choices[0]
        # A single outer pair may wrap the choice. Nested/ambiguous brackets
        # are left to the semantic pass rather than truncating a clause.
        before = "".join(re.findall(r"[()（）]", title[:choice.start()]))
        if before not in {"", "(", "（"}:
            continue
        start, end = heading.start("title") + choice.end(), heading.end()
        if before:
            closing = ")" if before == "(" else "）"
            end = source.find(closing, start, end)
            if end < 0:
                continue
        text = source[start:end]
        if re.search(r"[()（）\[\]【】、]", text):
            continue
        separators = list(re.finditer(r"[，,；;/／]", text))
        if len(separators) != 1:
            continue
        separator = start + separators[0].start()
        pair = []
        for left, right in ((start, separator), (separator + 1, end)):
            while left < right and source[left].isspace():
                left += 1
            while left < right and source[right - 1].isspace():
                right -= 1
            if left < right:
                pair.append((left, right, day))
        if len(pair) == 2:
            clauses.extend(pair)
    return clauses


def explicit_optional_labels(source: str) -> list[tuple[int, int]]:
    """Find only adjacent, explicit optional visit labels as original spans.

    This is a narrow omission/role check, not a place extractor or a POI
    creator. Suffixless names require the closed whole-day replacement form.
    Description, quotation, negation, addresses and common coffee drinks are
    rejected conservatively; no text is rewritten or joined across boundaries.
    """
    from app.trip_understanding.pipeline import atomic_place_rejection_reason

    spans: set[tuple[int, int]] = set()
    for pattern in (_OPTIONAL_AFTER, _OPTIONAL_BEFORE):
        for match in pattern.finditer(source):
            start, end = match.span("name")
            name = source[start:end]
            compact = "".join(name.split())
            if (
                atomic_place_rejection_reason(compact) is not None
                or _OPTIONAL_UNSAFE_NAME.search(compact)
                or (compact.endswith(("咖啡", "咖啡馆")) and _BEVERAGE_NAME.search(compact))
            ):
                continue
            left = max((source.rfind(mark, 0, start) for mark in "。！？；;\n"), default=-1) + 1
            stops = [source.find(mark, end) for mark in "。！？；;\n"]
            right = min((position + 1 for position in stops if position >= 0), default=len(source))
            context = source[left:right]
            if context.lstrip().startswith(">") or _OPTIONAL_UNSAFE_CONTEXT.search(context):
                continue
            spans.add((start, end))

    # A meal line naming two streets with a slash offers dining areas. Keep
    # both exact occurrences; a visit to the same street elsewhere is separate.
    for match in _MEAL_STREET_CHOICE.finditer(source):
        if (
            _OPTIONAL_UNSAFE_CONTEXT.search(match[0])
            or _MEAL_STREET_UNSAFE.search(match[0])
            or len(re.findall(r"[/／]", match[0])) != 1
            or source[:match.start()].count("```") % 2
            or re.search(r'(?:引用|引文|原文|示例|资料)[：:]\s*$|["“‘]\s*$', source[:match.start()])
        ):
            continue
        pair = [match.span("first"), match.span("second")]
        if any(
            atomic_place_rejection_reason(source[left:right]) is not None
            or _OPTIONAL_UNSAFE_NAME.search(source[left:right])
            or source[left:right] in {"大街", "小路", "道路", "街道", "胡同", "步行街"}
            for left, right in pair
        ):
            continue
        spans.update(pair)

    # Replacements are suggestions only with a literal condition/advice, or
    # an explicit "directly replace" inside an unselected day-choice scope.
    # A later decision is left to semantics instead of forcing OPTIONAL.
    if _WHOLE_DAY_SETTLED.search(source) or _SETTLED_CHOICE.search(source):
        return sorted(spans)
    scopes = choice_scopes(source)
    for match in _WHOLE_DAY_REPLACEMENT.finditer(source):
        start, end = match.span("name")
        name = source[start:end]
        if (
            atomic_place_rejection_reason(name) is not None
            or _OPTIONAL_UNSAFE_NAME.search(name)
            or _WHOLE_DAY_UNSAFE_NAME.search(name)
        ):
            continue
        left = max((source.rfind(mark, 0, match.start()) for mark in "。！？；;\n"), default=-1) + 1
        stops = [source.find(mark, match.end()) for mark in "。！？；;\n"]
        right = min((position + 1 for position in stops if position >= 0), default=len(source))
        context = source[left:right]
        prefix = source[left:match.start()]
        if context.lstrip().startswith(">") or _OPTIONAL_UNSAFE_CONTEXT.search(context):
            continue
        conditional = re.search(r"可以|如果|可选", prefix) is not None
        scoped_direct = re.search(r"直接[ \t]*$", prefix) is not None and any(
            scope_start <= match.start() and match.end() <= scope_end
            for scope_start, scope_end, _day in scopes
        )
        if conditional or scoped_direct:
            spans.add((start, end))
    return sorted(spans)


def explicit_visit_labels(source: str) -> list[tuple[int, int]]:
    """Locate explicit meal-street and walking clauses, without creating a POI.

    This only supports an omission check or a proposed mention's role. It
    assigns no day and does not select a branch: callers must still apply
    explicit choice scopes. Conditional, cancelled and descriptive language
    is deliberately left to the semantic pass.
    """
    from app.trip_understanding.pipeline import atomic_place_rejection_reason

    spans: set[tuple[int, int]] = set()
    for pattern in _VISIT_PATTERNS:
        for match in pattern.finditer(source):
            start, end = match.span("name")
            name = source[start:end]
            if atomic_place_rejection_reason(name) or _OPTIONAL_UNSAFE_NAME.search(name) or _VISIT_GENERIC_NAME.search(name):
                continue
            left = max((source.rfind(mark, 0, start) for mark in "。！？；;，,\n"), default=-1) + 1
            stops = [source.find(mark, match.end()) for mark in "。！？；;，,\n"]
            right = min((position + 1 for position in stops if position >= 0), default=len(source))
            context = source[left:right]
            sentence_stops = [source.find(mark, match.end()) for mark in "。！？；;\n"]
            sentence_end = min((position + 1 for position in sentence_stops if position >= 0), default=len(source))
            # A condition or cancellation can precede a comma and govern
            # this clause, or revoke it after a comma. Keep those warnings.
            sentence_start = max((source.rfind(mark, 0, left) for mark in "。！？；;\n"), default=-1) + 1
            prefix = source[sentence_start:left]
            if (
                context.lstrip().startswith(">")
                or _OPTIONAL_UNSAFE_CONTEXT.search(source[left:sentence_end])
                or _VISIT_SOFT_CONTEXT.search(context)
                or _OPTIONAL_UNSAFE_CONTEXT.search(prefix)
                or _VISIT_SOFT_CONTEXT.search(prefix)
                or source[:match.start()].count("```") % 2
                or re.search(r'(?:引用|引文|原文|示例|资料)[：:]\s*$|["“‘]\s*$', source[:match.start()])
                or ("meal_action" in match.groupdict() and re.search(
                    r"撤销|作废|如果|假如|要是|若(?:有|能|下雨)|有空|有时间|看情况|时间充裕|"
                    r"改到|改为|改期|延期|推迟|改天|另一天|择日|日期未定|以后再|下次再",
                    source[left:sentence_end],
                ))
            ):
                continue
            spans.add((start, end))
    return sorted(spans)
