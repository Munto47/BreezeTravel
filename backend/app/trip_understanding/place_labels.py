"""Remove only explicit non-identity annotations from literal place labels.

This helper does not identify places, expand abbreviations, or provide source
anchors. Callers must keep the original span and validate the resulting label.
Any unclassified bracket content leaves the entire original label unchanged.
"""
from __future__ import annotations

import re


_TRAILING_BRACKET = re.compile(
    r"(?P<base>[A-Za-z0-9\u4e00-\u9fff·—_ \-]+)"
    r"(?P<open>[（(])(?P<body>[^()（）\r\n]+)(?P<close>[）)])"
)
_FEE = re.compile(
    r"(?:(?:门票|票价|联票|费用|成人票)\s*[:：]?\s*)?"
    r"\d{1,5}(?:\.\d{1,2})?\s*元(?:\s*(?:/人|每人))?"
)
_ANNOTATIONS = frozenset({
    "免费", "免费预约", "免费开放", "免票", "免门票",
    "预约", "需预约", "需要预约", "须预约", "必须预约", "提前预约",
    "需提前预约", "需要提前预约", "须提前预约", "必须提前预约",
    "无需预约", "不用预约", "预约入场", "预约参观",
    "外观或购票入内", "外观或入内",
})
_QUALIFIER = re.compile(
    r"(?:[东南西北]{1,2}(?:馆|门|区)|[总主新老本]馆|"
    r"[一二三四五六七八九十\dA-Za-z]{1,3}号(?:馆|门)|"
    r"[A-Za-z0-9\u4e00-\u9fff·]{1,12}(?:分馆|分院|分店|校区|馆区|院区|广场馆|店))"
)
_UNSAFE_QUALIFIER_WORDS = (
    "另", "或", "与", "和", "以及", "备选", "改", "取消", "地址", "位于",
    "前往", "再去", "附近", "旁边", "对面", "号楼", "号院",
    "博物馆", "博物院", "美术馆", "图书馆", "科技馆", "纪念馆", "天文馆",
    "酒店", "宾馆", "饭店", "餐厅", "公园", "景区", "书店",
)


def _without_annotations(value: str) -> str:
    """Conservatively delete a known fee/booking note, retaining branch text.

    Only one trailing, correctly paired bracket group is supported. A branch
    qualifier must be the first item and every remaining item must be a known
    annotation. Labels containing unknown prose, an address, another place, a
    choice, rescheduling, or a URL remain byte-for-byte unchanged.
    """
    if len(value) > 160:
        return value
    match = _TRAILING_BRACKET.fullmatch(value)
    if match is None or {"（": "）", "(": ")"}[match["open"]] != match["close"]:
        return value
    items = [part.strip() for part in re.split(r"[，,；;、]", match["body"])]
    if any(not item for item in items):
        return value

    def is_annotation(item: str) -> bool:
        return item in _ANNOTATIONS or _FEE.fullmatch(item) is not None

    if all(is_annotation(item) for item in items):
        return match["base"]
    alias = re.fullmatch(r"(?:也叫|又称|又名|别名(?:是)?)([A-Za-z0-9\u4e00-\u9fff·]{2,24})", items[0])
    if len(items) == 1 and alias and not re.search(
        r"出来|进去|再去|前往|返回|然后|之后|取消|改为|改到|散步|拍照|打卡|参观|游览|看落日", alias[1],
    ):
        # Keep the source's principal name, never resolve its claimed alias.
        return match["base"]
    qualifier = items[0]
    floor = re.fullmatch(r"[A-Za-z\u4e00-\u9fff·]{2,16}\s*\d{1,3}\s*(?:楼|层)", qualifier)
    if floor and len(items) > 1 and all(is_annotation(item) or re.fullmatch(
        r"(?:咖啡|喝咖啡)?(?:看|俯瞰|欣赏)(?:城市|都市|江边|两岸)?(?:全景|夜景|江景|风景)", item,
    ) for item in items[1:]):
        # Keep the literal building/floor restriction; never turn a venue
        # inside a building into an unconstrained parent-name query.
        return match["base"] + match["open"] + qualifier + match["close"]
    if (
        len(items) < 2
        or _QUALIFIER.fullmatch(qualifier) is None
        or any(word in qualifier for word in _UNSAFE_QUALIFIER_WORDS)
        or not all(is_annotation(item) for item in items[1:])
    ):
        return value
    return match["base"] + match["open"] + qualifier + match["close"]


def normalized_place_label(value: str) -> str:
    """Normalize only source spelling, never substitute a place identity."""
    value = _without_annotations(value)
    # Spaces around a Latin brand or a floor number are formatting. Chinese
    # word spaces, prose, URLs and unknown parenthetical notes stay unchanged.
    if re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff·（）()—_ \t-]+", value):
        value = re.sub(r"(?<=[A-Za-z0-9])[ \t]+(?=[\u4e00-\u9fff])|(?<=[\u4e00-\u9fff])[ \t]+(?=[0-9])", "", value)
    # A model may retain the immediate action after an otherwise atomic road
    # label. Its full literal source span remains available to the compiler.
    match = re.fullmatch(r"(.+(?:步行街|滨江步道|大街|胡同|街|路))(?:逛街|散步|拍照|打卡)", value)
    return match[1] if match else value
