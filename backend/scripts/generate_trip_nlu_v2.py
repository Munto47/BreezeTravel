from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.trip_intake.models import (
    DateRangeExpression,
    EvidenceSpan,
    ExtractionIssue,
    IntakeReadiness,
    LocationEntityType,
    LocationExtraction,
    LocationMention,
    LocationRole,
    LocationStatus,
    PacePreference,
    PaceValue,
    PartialDate,
    PartyComposition,
    PartySizeExtraction,
    PreferenceExtraction,
    PreferenceItem,
    PreferencePolarity,
    PreferenceStatus,
    QuantifiedValue,
    QuantityDerivation,
    QuantityQuantifier,
    RequirementOperator,
    TemporalExtraction,
    TravelCommitment,
    TripIntakeExtraction,
    unknown_quantity,
    validate_extraction_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "eval_data" / "trip_nlu_v2"
PROMPT_SHA256 = "e87aacae03338340345747f1a65ec5a723ce142df0f0be8104ff2a74b15feb7a"

SPLITS = {
    "dev": {
        "count": 72,
        "difficulty": {"easy": 18, "medium": 32, "hard": 22},
        "destination": {"北京": 18, "上海": 18, "杭州": 18, "other": 8, "multiple": 4, "uncertain": 3, "missing": 3},
        "party": {"EXACT": 36, "RANGE": 11, "APPROXIMATE": 5, "AT_LEAST": 5, "AT_MOST": 5, "UNKNOWN": 10},
        "duration": {"EXACT": 32, "RANGE": 15, "APPROXIMATE": 5, "AT_LEAST": 5, "AT_MOST": 5, "UNKNOWN": 10},
        "minimums": {"preference": 58, "roles": 14, "interference": 18, "fictional": 8, "semantic_party": 14},
        "deterministic_families": 12,
    },
    "validation": {
        "count": 24,
        "difficulty": {"easy": 6, "medium": 11, "hard": 7},
        "destination": {"北京": 6, "上海": 6, "杭州": 6, "other": 2, "multiple": 1, "uncertain": 2, "missing": 1},
        "party": {"EXACT": 12, "RANGE": 4, "APPROXIMATE": 1, "AT_LEAST": 1, "AT_MOST": 1, "UNKNOWN": 5},
        "duration": {"EXACT": 11, "RANGE": 5, "APPROXIMATE": 1, "AT_LEAST": 1, "AT_MOST": 1, "UNKNOWN": 5},
        "minimums": {"preference": 19, "roles": 5, "interference": 6, "fictional": 2, "semantic_party": 5},
        "deterministic_families": 4,
    },
    "frozen_blind": {
        "count": 24,
        "difficulty": {"easy": 6, "medium": 11, "hard": 7},
        "destination": {"北京": 6, "上海": 6, "杭州": 6, "other": 2, "multiple": 1, "uncertain": 1, "missing": 2},
        "party": {"EXACT": 12, "RANGE": 3, "APPROXIMATE": 2, "AT_LEAST": 2, "AT_MOST": 2, "UNKNOWN": 3},
        "duration": {"EXACT": 11, "RANGE": 4, "APPROXIMATE": 2, "AT_LEAST": 2, "AT_MOST": 2, "UNKNOWN": 3},
        "minimums": {"preference": 19, "roles": 5, "interference": 6, "fictional": 2, "semantic_party": 5},
        "deterministic_families": 4,
    },
}

OTHER_CITIES = ["成都", "南京", "广州", "深圳", "西安", "厦门", "长沙", "昆明"]
FICTIONAL_PLACES = ["星河旧巷", "云顶水岸街", "北辰机械艺术馆", "松月湖畔市集"]
CITY_ALIASES = {"北京": "帝都", "上海": "魔都", "杭州": "杭城"}
CITY_TYPOS = {"北京": "北亰", "上海": "上诲", "杭州": "杭洲"}
LIKES = ["历史文化", "自然风景", "美食探店", "购物逛街", "夜景", "夜生活", "摄影打卡", "博物馆展览", "城市漫步", "亲子娱乐", "情侣约会", "二次元", "科技体验"]
DISLIKES = ["排队", "人挤人", "早起", "长距离步行", "爬山", "辣食", "商场购物", "夜间活动", "网红景点", "高频换乘", "高消费", "室外暴晒"]
FAMILY_PHRASES = [
    "下面是从家庭群里摘出的当前需求，转发时夹进了闲聊，请只按明确陈述记录字段",
    "刚结束语音讨论，我把大家真正达成一致的部分重新写成一段，语气词不用当条件",
    "备忘录里混着以前搜索过的内容，这里重新说明现在要保存的旅行需求和未知项",
    "这条来自聊天截图的文字版，换行位置不可靠，但句子里的肯定、否定关系都要保留",
    "先交代一下上下文：大家改过几次主意，以下出现取消或过去时的地方只是背景信息",
    "我按电话讨论结果重新口述，可能没有标点，抽取时不要用常见人数或天数替代缺失值",
    "这不是请你做攻略，只是把已经说清和仍未说清的条件忠实登记，尤其别擅自补默认项",
    "群消息顺序有点乱，我把有效信息放在同一条里，地点角色和数字所属类别需要分别判断",
    "从临时便签复制过来时丢了项目符号，请依据原句关系处理，而不是按词出现顺序猜意图",
    "家里讨论时提到了不少无关数字，真正的同行规模、停留时长和预算不要相互串字段",
    "票还没有购买，这段只是确认需求草稿；任何没有确定的量都应继续保持不确定状态",
    "请把这一段当作结构抽取输入，英文插词只是聊天习惯，不能改变中文约束的肯定或否定",
    "刚才那条有错别字所以我完整重发，旧地点若明确取消就不能升级成这次旅行的目的地",
    "我把几段微信合在一起了，同一个名字可能承担不同地点角色，需要跟随上下文而非热度",
    "语音转写没有自动加标点，仍然要区分出发、返程、想去、排除以及仅用于比较的地方",
    "这是同伴最后整理的文字记录，表达比较口语，但范围、约数、上限和下限不能都算精确值",
    "背景说明很多，不过任务仍然只是保存需求；虚构或陌生地点也要保留原文，不要自行验真",
    "先列当前硬条件再夹带聊天补充，抽取结果要能指出冲突，而不是挑一个看起来更合理的数字",
    "同行人可能临时变化，所以文本怎样表达就怎样保存；标签只能来自原话或可解释的语义计算",
    "我把攻略讨论删掉后留下这段输入，尚未提及的偏好不是明确无偏好，两者必须严格区分",
    "计划反复修改过，这次请识别取消、改去和最终确认的方向，不要把最早出现的地点当结论",
    "同学群里的消息很碎，我用一条长句串起来；重复词和表情不会改变证据在原文中的位置",
    "标题里的城市曾经写错，正文会明确当前说法，规范化名称可以填但原始称呼必须同时保留",
    "我再复述一次真实需求，所有推断都要能回到对应原句，找不到证据时宁可标记需要确认",
    "这一段中英文混着写，please只是语气，人数、日期和地点仍按中文上下文解释",
    "表情符号只是停顿😊 后面的证据偏移仍须按完整原文字符计算，不能按字节或代码单元偷换",
    "前半部分会出现过去旅行的回忆，只有明确指向本次计划的内容才能成为当前目的地或偏好",
    "后面才补充了最终约束，但前面未被否定的事实依然有效，不能用一句总结把整段全部作废",
    "车次、发车时刻、房间和票数都可能出现，请先判断数字语义再写入人数或旅行天数",
    "预算金额和同行人数挨得很近，抽取时需要保持字段边界，并保留每个原子值自己的证据",
    "儿童、老人或宠物条件只在原话出现时记录，不能从亲属称谓自动断言年龄或特殊身份",
    "若文本直接说尚未确定，应记录显式未知及其证据，不得为了通过后续流程补成常用值",
]


def expanded(quota: dict[str, int]) -> list[str]:
    return [value for value, count in quota.items() for _ in range(count)]


def stable_spread(values: list[Any], seed: str) -> list[Any]:
    decorated = [
        (hashlib.sha256(f"{seed}:{index}:{value}".encode()).hexdigest(), value)
        for index, value in enumerate(values)
    ]
    return [value for _, value in sorted(decorated)]


def span(text: str, source_id: str, quote: str, *, last: bool = False) -> EvidenceSpan:
    start = text.rfind(quote) if last else text.find(quote)
    if start < 0:
        raise ValueError(f"evidence quote missing: {quote!r}")
    return EvidenceSpan(source_id=source_id, start=start, end=start + len(quote), quote=quote)


def quantity(
    kind: str,
    text: str,
    source_id: str,
    quote: str | None,
    *,
    value: int,
    derivation: QuantityDerivation,
) -> QuantifiedValue:
    evidence = [span(text, source_id, quote)] if quote else []
    if kind == "EXACT":
        return QuantifiedValue(min=value, max=value, quantifier=kind, derivation=derivation, evidence=evidence)
    if kind == "RANGE":
        return QuantifiedValue(min=value, max=value + 2, quantifier=kind, derivation=derivation, evidence=evidence)
    if kind == "APPROXIMATE":
        return QuantifiedValue(min=value, max=value, quantifier=kind, derivation=derivation, evidence=evidence)
    if kind == "AT_LEAST":
        return QuantifiedValue(min=value, quantifier=kind, derivation=derivation, evidence=evidence)
    if kind == "AT_MOST":
        return QuantifiedValue(max=value, quantifier=kind, derivation=derivation, evidence=evidence)
    return QuantifiedValue(
        quantifier=QuantityQuantifier.UNKNOWN,
        derivation=QuantityDerivation.MISSING,
        evidence=evidence,
    )


def party_phrase(kind: str, semantic: bool, index: int) -> tuple[str, int, QuantityDerivation, PartyComposition]:
    semantic_forms = [
        ("我和对象", 2, ["情侣"]),
        ("两大一小", 3, ["家庭", "亲子"]),
        ("我、爸妈和妹妹", 4, ["家庭"]),
        ("我和两个朋友", 3, ["朋友"]),
        ("两对情侣", 4, ["情侣"]),
        ("我自己", 1, ["独自"]),
    ]
    if kind == "EXACT" and semantic:
        phrase, value, tags = semantic_forms[index % len(semantic_forms)]
        return phrase, value, QuantityDerivation.SEMANTIC_INFERENCE, PartyComposition(tags=tags)
    value = index % 7 + 1
    if kind == "EXACT":
        return f"{value}人", value, QuantityDerivation.EXPLICIT_COUNT, PartyComposition()
    if kind == "RANGE":
        return f"{value}到{value + 2}人", value, QuantityDerivation.EXPLICIT_COUNT, PartyComposition()
    if kind == "APPROXIMATE":
        return f"大概{value}个人", value, QuantityDerivation.EXPLICIT_COUNT, PartyComposition()
    if kind == "AT_LEAST":
        return f"至少{value}人", value, QuantityDerivation.EXPLICIT_COUNT, PartyComposition()
    if kind == "AT_MOST":
        return f"最多{value}人", value, QuantityDerivation.EXPLICIT_COUNT, PartyComposition()
    return "人数还没定，可能有人临时加入", value, QuantityDerivation.MISSING, PartyComposition(tags=["同行人员尚未确定"])


def duration_phrase(kind: str, index: int, use_date_range: bool) -> tuple[str, int, QuantityDerivation]:
    value = [1, 2, 3, 4, 5, 7][index % 6]
    if kind == "EXACT" and use_date_range:
        return "10月3日到10月5日", 3, QuantityDerivation.DATE_RANGE
    if kind == "EXACT":
        return f"玩{value}天", value, QuantityDerivation.EXPLICIT_COUNT
    if kind == "RANGE":
        return f"玩{value}到{value + 2}天", value, QuantityDerivation.EXPLICIT_COUNT
    if kind == "APPROXIMATE":
        return f"大概玩{value}天", value, QuantityDerivation.EXPLICIT_COUNT
    if kind == "AT_LEAST":
        return f"至少待{value}天", value, QuantityDerivation.EXPLICIT_COUNT
    if kind == "AT_MOST":
        return f"最多待{value}天", value, QuantityDerivation.EXPLICIT_COUNT
    return "时间还没定，有空就多待几天", value, QuantityDerivation.MISSING


def destination_phrase(bucket: str, roles: bool, index: int) -> tuple[str, list[tuple[str, str, LocationRole]], str | None, LocationStatus]:
    if bucket in {"北京", "上海", "杭州"}:
        city = bucket
    elif bucket == "other":
        city = OTHER_CITIES[index % len(OTHER_CITIES)]
    else:
        city = None
    if city and roles:
        raw_city = (
            CITY_TYPOS[city]
            if index % 19 == 0 and city in CITY_TYPOS
            else CITY_ALIASES.get(city, city) if index % 5 == 0 else city
        )
        phrase = f"从广州出发，去年去过西安，本来想去天津后来取消，这次确定改去{raw_city}，不去重庆，最后返程回深圳"
        mentions = [
            ("广州", "广州市", LocationRole.ORIGIN),
            ("西安", "西安市", LocationRole.OTHER_MENTION),
            ("天津", "天津市", LocationRole.OTHER_MENTION),
            (raw_city, f"{city}市", LocationRole.PRIMARY_DESTINATION),
            ("重庆", "重庆市", LocationRole.EXCLUDED),
            ("深圳", "深圳市", LocationRole.RETURN_LOCATION),
        ]
        return phrase, mentions, city, LocationStatus.EXACT
    if city:
        raw_city = (
            CITY_TYPOS[city]
            if index % 19 == 0 and city in CITY_TYPOS
            else CITY_ALIASES.get(city, city) if index % 5 == 0 else city
        )
        return f"这次目的地是{raw_city}", [(raw_city, f"{city}市", LocationRole.PRIMARY_DESTINATION)], city, LocationStatus.EXACT
    if bucket == "multiple":
        return (
            "北京或者上海都可以，还没二选一",
            [
                ("北京", "北京市", LocationRole.DESTINATION_CANDIDATE),
                ("上海", "上海市", LocationRole.DESTINATION_CANDIDATE),
            ],
            None,
            LocationStatus.MULTIPLE,
        )
    if bucket == "uncertain":
        return (
            "有人说去杭州也有人坚持北京，目前矛盾没解决",
            [
                ("杭州", "杭州市", LocationRole.DESTINATION_CANDIDATE),
                ("北京", "北京市", LocationRole.DESTINATION_CANDIDATE),
            ],
            None,
            LocationStatus.UNCERTAIN,
        )
    if roles:
        return "我现在在上海，去年去过北京，但这次目的地没说", [
            ("上海", "上海市", LocationRole.ORIGIN),
            ("北京", "北京市", LocationRole.OTHER_MENTION),
        ], None, LocationStatus.MISSING
    return "目的地还没想好", [], None, LocationStatus.MISSING


def build_case(
    case_number: int,
    split: str,
    local_index: int,
    destination_bucket: str,
    party_kind: str,
    duration_kind: str,
    difficulty: str,
    flags: dict[str, bool],
    family_id: str,
    generator_source: str,
    prompt_offset: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    case_id = f"TRIP_NLU_{case_number:04d}"
    source_id = f"{case_id}:text"
    destination_text, mention_specs, primary_city, location_status = destination_phrase(
        destination_bucket, flags["roles"], case_number
    )
    party_text, party_value, party_derivation, composition = party_phrase(
        party_kind, flags["semantic_party"], case_number
    )
    use_date_range = duration_kind == "EXACT" and case_number % 9 == 0
    duration_text, duration_value, duration_derivation = duration_phrase(
        duration_kind, case_number, use_date_range
    )
    family_context = FAMILY_PHRASES[(case_number - 1) // 3]
    parts = [family_context, destination_text, party_text, duration_text]
    fictional_place = FICTIONAL_PLACES[case_number % len(FICTIONAL_PLACES)] if flags["fictional"] else None
    if fictional_place:
        parts.append(f"还想去{fictional_place}，名字别纠正")

    like = LIKES[case_number % len(LIKES)]
    dislike = DISLIKES[case_number % len(DISLIKES)]
    pace_value = [PaceValue.RELAXED, PaceValue.BALANCED, PaceValue.INTENSIVE][case_number % 3]
    pace_quote = {
        PaceValue.RELAXED: "慢慢玩别排太满",
        PaceValue.BALANCED: "节奏适中别太赶也别太松",
        PaceValue.INTENSIVE: "想高密度打卡",
    }[pace_value]
    requirement_themes = [
        ("physical", "少走路", RequirementOperator.MAX, "LOW_WALKING", None, "全员"),
        ("accommodation", "住宿靠近地铁", RequirementOperator.REQUIRED, "NEAR_TRANSIT", None, None),
        ("dietary", "不要辣", RequirementOperator.AVOID, "SPICY", None, "全员"),
        ("children", "儿童友好", RequirementOperator.REQUIRED, True, None, "儿童"),
        ("elderly", "老人友好", RequirementOperator.REQUIRED, True, None, "老人"),
        ("pet", "宠物友好", RequirementOperator.REQUIRED, True, None, "宠物"),
        ("accessibility", "全程无障碍", RequirementOperator.REQUIRED, True, None, "轮椅使用者"),
        ("time", "最后一天中午返程", RequirementOperator.REQUIRED, "LAST_DAY_NOON", None, "全员"),
    ]
    requirement_category, requirement_quote, requirement_operator, requirement_value, requirement_unit, requirement_applies_to = requirement_themes[
        case_number % len(requirement_themes)
    ]
    if flags["preference"]:
        parts.append(
            f"喜欢{like}，避开{dislike}，{pace_quote}，总预算不超过2000元并且公共交通优先，{requirement_quote}"
        )
    explicit_no_preference = not flags["preference"] and case_number % 5 == 0
    if explicit_no_preference:
        parts.append("这次明确没有任何偏好")
    if flags["interference"]:
        parts.append("补充干扰：G123次18点发车，孩子6岁，订2间房，买4张票，预算另记2000元")
    arrival_quote = "预计早上到达" if case_number % 13 == 0 else None
    if arrival_quote:
        parts.append(arrival_quote)
    if difficulty == "hard":
        parts.append("注意：只有明确标成过去、取消或旧计划的内容无效，其他当前陈述仍然有效😅 please别把数字串类")
    elif difficulty == "medium":
        parts.append("嗯大概就是这样😊")
    closings_a = ["这回", "目前", "认真说", "顺手记", "家里定", "群里定", "买票前", "刚确认", "临时记", "最终版", "此刻", "现在"]
    closings_b = ["就这些", "别脑补", "按原话", "先这样", "以此为准", "别改写", "保留未知", "只抽字段", "原样记录", "别做攻略"]
    parts.append(
        f"{closings_a[(case_number - 1) // 10]}{closings_b[(case_number - 1) % 10]}"
    )
    if generator_source == "USER_PROMPT":
        prompt_leads = [
            "【需求原话】",
            "语音稿：呃，",
            "聊天合并｜",
            "request says: ",
            "只抽结构→",
        ]
        prompt_tails = [
            "以上只做需求抽取，不规划路线。",
            "没写清的保持未知，over。",
            "请保留原话证据，谢谢。",
            "不要验证地名真假。",
            "这就是完整输入。",
        ]
        body = ("\n— ".join(parts) if case_number % 2 else " ... ".join(reversed(parts)))
        text = (
            f"{prompt_leads[(case_number + prompt_offset) % len(prompt_leads)]}{body}\n"
            f"{prompt_tails[(case_number + prompt_offset) % len(prompt_tails)]}"
        )
    elif case_number % 3 == 0:
        text = "\n".join(parts)
    elif case_number % 3 == 1:
        text = "；".join(parts)
    else:
        text = "，另外".join([parts[0], *reversed(parts[1:])])
    if case_number % 17 == 0:
        punctuation_map = str.maketrans({"，": " ", "；": " ", "。": " ", "：": " ", "、": " "})
        text = text.translate(punctuation_map)
        party_text = party_text.translate(punctuation_map)
        duration_text = duration_text.translate(punctuation_map)

    mentions: list[LocationMention] = []
    primary_id = None
    for mention_index, (raw, normalized, role) in enumerate(mention_specs, start=1):
        mention_id = f"location-{mention_index}"
        mentions.append(
            LocationMention(
                mention_id=mention_id,
                raw_text=raw,
                normalized_name=normalized,
                country_code="CN",
                entity_type=LocationEntityType.CITY,
                role=role,
                confidence=1,
                evidence=[
                    span(
                        text,
                        source_id,
                        raw,
                        last=role == LocationRole.PRIMARY_DESTINATION and text.count(raw) > 1,
                    )
                ],
            )
        )
        if role == LocationRole.PRIMARY_DESTINATION:
            primary_id = mention_id
    if fictional_place:
        mentions.append(
            LocationMention(
                mention_id=f"location-{len(mentions) + 1}",
                raw_text=fictional_place,
                entity_type=LocationEntityType.PLACE,
                role=LocationRole.REQUESTED_PLACE,
                confidence=1,
                evidence=[span(text, source_id, fictional_place)],
            )
        )

    party = quantity(
        party_kind,
        text,
        source_id,
        party_text,
        value=party_value,
        derivation=party_derivation,
    )
    days = quantity(
        duration_kind,
        text,
        source_id,
        duration_text,
        value=duration_value,
        derivation=duration_derivation,
    )
    nights = unknown_quantity()
    if case_number % 10 == 0:
        nights_quote = "住2晚"
        text += f"；{nights_quote}，晚数单独算"
        nights = QuantifiedValue(
            min=2,
            max=2,
            quantifier=QuantityQuantifier.EXACT,
            derivation=QuantityDerivation.EXPLICIT_COUNT,
            evidence=[span(text, source_id, nights_quote)],
        )
    date_range = None
    if use_date_range:
        date_range = DateRangeExpression(
            raw_text=duration_text,
            start=PartialDate(month=10, day=3),
            end=PartialDate(month=10, day=5),
            evidence=[span(text, source_id, duration_text)],
        )
    arrival = (
        TravelCommitment(
            at_text="早上",
            evidence=[span(text, source_id, arrival_quote)],
        )
        if arrival_quote
        else None
    )
    departure = (
        TravelCommitment(
            at_text="最后一天中午",
            evidence=[span(text, source_id, requirement_quote)],
        )
        if flags["preference"] and requirement_category == "time"
        else None
    )

    if party_text == "两大一小":
        composition = PartyComposition(
            adults=QuantifiedValue(
                min=2,
                max=2,
                quantifier=QuantityQuantifier.EXACT,
                derivation=QuantityDerivation.SEMANTIC_INFERENCE,
                evidence=[span(text, source_id, party_text)],
            ),
            children=QuantifiedValue(
                min=1,
                max=1,
                quantifier=QuantityQuantifier.EXACT,
                derivation=QuantityDerivation.SEMANTIC_INFERENCE,
                evidence=[span(text, source_id, party_text)],
            ),
            tags=composition.tags,
        )

    preference = PreferenceExtraction()
    if flags["preference"]:
        preference = PreferenceExtraction(
            status=PreferenceStatus.SPECIFIED,
            items=[
                PreferenceItem(
                    item_id="preference-like",
                    category="experience",
                    label=like,
                    polarity=PreferencePolarity.LIKE,
                    confidence=1,
                    evidence=[span(text, source_id, f"喜欢{like}")],
                ),
                PreferenceItem(
                    item_id="preference-dislike",
                    category="avoidance",
                    label=dislike,
                    polarity=PreferencePolarity.DISLIKE,
                    confidence=1,
                    evidence=[span(text, source_id, f"避开{dislike}")],
                ),
                PreferenceItem(
                    item_id="requirement-budget",
                    category="budget",
                    label="总预算",
                    polarity=PreferencePolarity.REQUIREMENT,
                    operator=RequirementOperator.MAX,
                    value=2000,
                    unit="元",
                    currency="CNY",
                    confidence=1,
                    evidence=[span(text, source_id, "总预算不超过2000元")],
                ),
                PreferenceItem(
                    item_id="requirement-transport",
                    category="transport",
                    label="公共交通优先",
                    polarity=PreferencePolarity.REQUIREMENT,
                    operator=RequirementOperator.PREFER,
                    value="PUBLIC_TRANSIT",
                    confidence=1,
                    evidence=[span(text, source_id, "公共交通优先")],
                ),
                PreferenceItem(
                    item_id="requirement-themed",
                    category=requirement_category,
                    label=requirement_quote,
                    polarity=PreferencePolarity.REQUIREMENT,
                    operator=requirement_operator,
                    value=requirement_value,
                    unit=requirement_unit,
                    applies_to=requirement_applies_to,
                    confidence=1,
                    evidence=[span(text, source_id, requirement_quote)],
                ),
            ],
            pace=PacePreference(
                value=pace_value,
                confidence=1,
                evidence=[span(text, source_id, pace_quote)],
            ),
        )
    elif explicit_no_preference:
        no_preference_span = span(text, source_id, "明确没有任何偏好")
        preference = PreferenceExtraction(
            status=PreferenceStatus.NO_PREFERENCE,
            pace=PacePreference(
                value=PaceValue.NO_PREFERENCE,
                confidence=1,
                evidence=[no_preference_span],
            ),
            no_preference_evidence=[no_preference_span],
        )

    issues = []
    if location_status != LocationStatus.EXACT:
        issues.append(
            ExtractionIssue(
                code="DESTINATION_NEEDS_CONFIRMATION",
                field_path="locations.primary_city",
                message="目的地不是单一精确城市",
                evidence=(
                    [span for mention in mentions for span in mention.evidence]
                    or [span(text, source_id, destination_text)]
                ),
            )
        )
    else:
        primary = next(item for item in mentions if item.mention_id == primary_id)
        issues.append(
            ExtractionIssue(
                code="PRIMARY_CITY_CONFIRMATION_REQUIRED",
                field_path="locations.primary_city",
                message="主城市尚未由用户确认",
                evidence=primary.evidence,
            )
        )
    issues.append(
        ExtractionIssue(
            code=(
                "PARTY_SIZE_CONFIRMATION_REQUIRED"
                if party_kind == "EXACT"
                else "PARTY_SIZE_NEEDS_CONFIRMATION"
            ),
            field_path="party_size.total",
            message=("人数尚未由用户确认" if party_kind == "EXACT" else "人数不是精确值，需要用户确认"),
            evidence=party.evidence,
        )
    )
    issues.append(
        ExtractionIssue(
            code="DATE_RANGE_MISSING_OR_INCOMPLETE",
            field_path="temporal.date_range",
            message="缺少含年份的完整日期范围，需要用户确认",
            evidence=(date_range.evidence if date_range else days.evidence),
        )
    )
    if duration_kind != "EXACT":
        issues.append(
            ExtractionIssue(
                code="DURATION_NEEDS_CONFIRMATION",
                field_path="temporal.days",
                message="旅行天数不是精确值，需要用户确认",
                evidence=days.evidence,
            )
        )
    if nights.quantifier == QuantityQuantifier.EXACT:
        upper_days = days.max
        if upper_days is not None and nights.min is not None and nights.min >= upper_days:
            issues.append(
                ExtractionIssue(
                    code="DAYS_NIGHTS_CONFLICT",
                    field_path="temporal",
                    message="明确晚数与天数上界冲突，需要用户确认",
                    evidence=[*days.evidence, *nights.evidence],
                )
            )
    extraction = TripIntakeExtraction(
        locations=LocationExtraction(
            mentions=mentions,
            primary_mention_id=primary_id,
            status=location_status,
        ),
        party_size=PartySizeExtraction(total=party, composition=composition),
        temporal=TemporalExtraction(
            days=days,
            nights=nights,
            date_range=date_range,
            arrival=arrival,
            departure=departure,
        ),
        preferences=preference,
        issues=issues,
        readiness=IntakeReadiness.NEEDS_CONFIRMATION,
    )
    validate_extraction_evidence(extraction, {source_id: text})
    noise_types = []
    if flags["roles"]:
        noise_types.extend(["location_roles", "negation_and_correction"])
    if flags["interference"]:
        noise_types.append("numeric_interference")
    if flags["fictional"]:
        noise_types.append("fictional_location")
    if flags["semantic_party"]:
        noise_types.append("semantic_party_calculation")
    if party_kind == "UNKNOWN" or duration_kind == "UNKNOWN":
        noise_types.append("explicit_unknown")
    if party_kind == "RANGE" or duration_kind == "RANGE":
        noise_types.append("range_quantity")
    if use_date_range:
        noise_types.append("date_range_inclusive")
    if nights.quantifier == QuantityQuantifier.EXACT:
        noise_types.append("independent_nights")
    if "😊" in text or "😅" in text:
        noise_types.append("emoji")
    if "\n" in text:
        noise_types.append("multiline")
    if "please" in text or "request says" in text or "over" in text:
        noise_types.append("mixed_language")
    if not any(mark in text for mark in "，；。：、"):
        noise_types.append("no_punctuation")
    if any(typo in text for typo in CITY_TYPOS.values()):
        noise_types.append("typo")
    annotation = {
        "difficulty": difficulty,
        "noise_types": sorted(set(noise_types)),
        "source_family_id": family_id,
        "template_family_id": f"template:{family_id}",
        "generator_family_id": f"generator:{family_id}",
        "renderer_id": (
            "renderer:prompt-contract-v2" if generator_source == "USER_PROMPT" else "renderer:deterministic-v2"
        ),
        "generator_source": generator_source,
        "coverage": {
            "destination": destination_bucket,
            "party": party_kind,
            "duration": duration_kind,
            **flags,
            "preference": flags["preference"] or explicit_no_preference,
        },
    }
    labelled = {
        "case_id": case_id,
        "input_text": text,
        "source_id": source_id,
        "expected": extraction.model_dump(mode="json"),
        "annotation": annotation,
    }
    blind_input = {
        "case_id": case_id,
        "input_text": text,
        "source_id": source_id,
    }
    return labelled, blind_input


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for value in values)
    path.write_text(content, encoding="utf-8", newline="\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def coverage(cases: list[dict[str, Any]]) -> dict[str, Any]:
    hard = [item for item in cases if item["annotation"]["difficulty"] == "hard"]
    return {
        "count": len(cases),
        "difficulty": dict(Counter(item["annotation"]["difficulty"] for item in cases)),
        "destination": dict(Counter(item["annotation"]["coverage"]["destination"] for item in cases)),
        "party": dict(Counter(item["annotation"]["coverage"]["party"] for item in cases)),
        "duration": dict(Counter(item["annotation"]["coverage"]["duration"] for item in cases)),
        "generator_source": dict(Counter(item["annotation"]["generator_source"] for item in cases)),
        "minimums": {
            key: sum(bool(item["annotation"]["coverage"][key]) for item in cases)
            for key in ("preference", "roles", "interference", "fictional", "semantic_party")
        },
        "requirement_categories": dict(
            Counter(
                item["category"]
                for case in cases
                for item in case["expected"]["preferences"]["items"]
                if item["polarity"] == "REQUIREMENT"
            )
        ),
        "hard_fields": {
            "destination_non_exact": sum(
                item["annotation"]["coverage"]["destination"]
                in {"multiple", "uncertain", "missing"}
                for item in hard
            ),
            "party_unknown_or_range": sum(
                item["annotation"]["coverage"]["party"] in {"UNKNOWN", "RANGE"}
                for item in hard
            ),
            "duration_unknown_or_range": sum(
                item["annotation"]["coverage"]["duration"] in {"UNKNOWN", "RANGE"}
                for item in hard
            ),
        },
    }


def generate(
    data_root: Path,
    external_blind_labels: Path,
    user_prompt_path: Path,
) -> None:
    repo_root = ROOT.parent.resolve()
    external_blind_labels = external_blind_labels.resolve()
    try:
        external_blind_labels.relative_to(repo_root)
    except ValueError:
        pass
    else:
        raise ValueError("external blind labels must be outside the repository")
    external_blind_labels = external_blind_labels.resolve(strict=True)
    user_prompt_bytes = user_prompt_path.read_bytes()
    user_prompt = user_prompt_bytes.decode("utf-8")
    actual_prompt_hash = hashlib.sha256(user_prompt_bytes).hexdigest()
    if actual_prompt_hash != PROMPT_SHA256:
        raise ValueError("user prompt hash does not match the approved generation contract")
    prompt_offset = int(actual_prompt_hash[:8], 16)
    all_cases: dict[str, list[dict[str, Any]]] = {"dev": [], "validation": []}
    case_number = 1
    family_registry = []
    source_registry = []
    for split in ("dev", "validation"):
        config = SPLITS[split]
        split_salt = "public"
        destinations = stable_spread(expanded(config["destination"]), f"{split}:{split_salt}:destination:v2")
        parties = stable_spread(expanded(config["party"]), f"{split}:{split_salt}:party:v2")
        durations = stable_spread(expanded(config["duration"]), f"{split}:{split_salt}:duration:v2")
        flag_positions: dict[str, set[int]] = {}
        for flag in ("preference", "roles", "interference", "fictional"):
            ranked = stable_spread(list(range(config["count"])), f"{split}:{split_salt}:{flag}:v2")
            flag_positions[flag] = set(ranked[: config["minimums"][flag]])
        exact_positions = [index for index, value in enumerate(parties) if value == "EXACT"]
        semantic_positions = set(
            stable_spread(exact_positions, f"{split}:{split_salt}:semantic-party:v2")[
                : config["minimums"]["semantic_party"]
            ]
        )
        ranked_complexity = sorted(
            range(config["count"]),
            key=lambda index: (
                (5 if destinations[index] in {"multiple", "uncertain", "missing"} else 0)
                + (4 if parties[index] == "UNKNOWN" else 2 if parties[index] != "EXACT" else 0)
                + (4 if durations[index] == "UNKNOWN" else 2 if durations[index] != "EXACT" else 0)
                + (1 if index in flag_positions["roles"] else 0)
                + (1 if index in flag_positions["interference"] else 0)
                + (1 if index in flag_positions["fictional"] else 0),
                hashlib.sha256(f"{split}:{split_salt}:difficulty:{index}".encode()).hexdigest(),
            ),
            reverse=True,
        )
        hard_positions = set(ranked_complexity[: config["difficulty"]["hard"]])
        easy_positions = set(ranked_complexity[-config["difficulty"]["easy"] :])
        difficulties = [
            "hard" if index in hard_positions else "easy" if index in easy_positions else "medium"
            for index in range(config["count"])
        ]
        family_count = config["count"] // 3
        for family_index in range(family_count):
            generator_source = (
                "DETERMINISTIC"
                if family_index < config["deterministic_families"]
                else "USER_PROMPT"
            )
            family_id = f"{split.upper()}_{'D' if generator_source == 'DETERMINISTIC' else 'P'}_{family_index + 1:02d}"
            family_registry.append(
                {
                    "family_id": family_id,
                    "split": split,
                    "generator_source": generator_source,
                    "prompt_sha256": PROMPT_SHA256 if generator_source == "USER_PROMPT" else None,
                    "renderer_id": (
                        "renderer:prompt-contract-v2"
                        if generator_source == "USER_PROMPT"
                        else "renderer:deterministic-v2"
                    ),
                    "generator_family_id": f"generator:{family_id}",
                    "template_family_id": f"template:{family_id}",
                    "template_sha256": hashlib.sha256(
                        (
                            FAMILY_PHRASES[(case_number - 1) // 3]
                            + ("prompt-contract-v2" if generator_source == "USER_PROMPT" else "deterministic-v2")
                        ).encode()
                    ).hexdigest(),
                    "case_count": 3,
                    "case_ids": [
                        f"TRIP_NLU_{case_number + family_offset:04d}"
                        for family_offset in range(3)
                    ],
                }
            )
            for offset in range(3):
                local_index = family_index * 3 + offset
                party_kind = parties[local_index]
                flags = {
                    "preference": local_index in flag_positions["preference"],
                    "roles": local_index in flag_positions["roles"],
                    "interference": local_index in flag_positions["interference"],
                    "fictional": local_index in flag_positions["fictional"],
                    "semantic_party": local_index in semantic_positions,
                }
                labelled, _ = build_case(
                    case_number,
                    split,
                    local_index,
                    destinations[local_index],
                    party_kind,
                    durations[local_index],
                    difficulties[local_index],
                    flags,
                    family_id,
                    generator_source,
                    prompt_offset,
                )
                all_cases[split].append(labelled)
                source_registry.append(
                    {
                        "case_id": labelled["case_id"],
                        "split": split,
                        "generator_source": generator_source,
                        "family_id": family_id,
                        "prompt_sha256": PROMPT_SHA256 if generator_source == "USER_PROMPT" else None,
                    }
                )
                case_number += 1

    blind_labels = read_jsonl(external_blind_labels)
    expected_blind_ids = [f"TRIP_NLU_{index:04d}" for index in range(97, 121)]
    if [item.get("case_id") for item in blind_labels] != expected_blind_ids:
        raise ValueError("external blind labels must contain TRIP_NLU_0097 through TRIP_NLU_0120")
    blind_inputs = []
    blind_families: dict[str, list[dict[str, Any]]] = {}
    for item in blind_labels:
        extraction = TripIntakeExtraction.model_validate(item["expected"])
        validate_extraction_evidence(extraction, {item["source_id"]: item["input_text"]})
        blind_inputs.append(
            {
                "case_id": item["case_id"],
                "input_text": item["input_text"],
                "source_id": item["source_id"],
            }
        )
        family_id = item["annotation"]["source_family_id"]
        blind_families.setdefault(family_id, []).append(item)
        generator_source = item["annotation"]["generator_source"]
        source_registry.append(
            {
                "case_id": item["case_id"],
                "split": "frozen_blind",
                "generator_source": generator_source,
                "family_id": family_id,
                "prompt_sha256": PROMPT_SHA256 if generator_source == "USER_PROMPT" else None,
                "recipe_location": "EXTERNAL_SEALED",
            }
        )
    for family_id, cases in blind_families.items():
        if len(cases) != 3:
            raise ValueError("each external blind family must contain exactly three cases")
        generator_source = cases[0]["annotation"]["generator_source"]
        if any(item["annotation"]["generator_source"] != generator_source for item in cases):
            raise ValueError("external blind family mixes generator sources")
        family_registry.append(
            {
                "family_id": family_id,
                "split": "frozen_blind",
                "generator_source": generator_source,
                "prompt_sha256": PROMPT_SHA256 if generator_source == "USER_PROMPT" else None,
                "case_count": 3,
                "case_ids": [item["case_id"] for item in cases],
                "recipe_location": "EXTERNAL_SEALED",
            }
        )

    data_root.mkdir(parents=True, exist_ok=True)
    write_jsonl(data_root / "dev.jsonl", all_cases["dev"])
    write_jsonl(data_root / "validation.jsonl", all_cases["validation"])
    write_jsonl(data_root / "frozen_blind.inputs.jsonl", blind_inputs)
    write_jsonl(data_root / "source_registry.jsonl", source_registry)
    write_jsonl(data_root / "family_registry.jsonl", family_registry)
    write_json(
        data_root / "generator_registry.json",
        {
            "schema_version": "trip-nlu-generator-registry-v2",
            "truth_first": True,
            "deterministic_cases": 60,
            "user_prompt_guided_cases": 60,
            "user_prompt_sha256": PROMPT_SHA256,
            "user_prompt_code_points": len(user_prompt),
            "user_prompt_generation_method": "PROMPT_CONTRACT_COMPILED_RENDERER",
            "prompt_contract_applied_to_renderer": True,
            "tool_calls": False,
            "provider_validation": False,
            "blind_generation_recipe_in_repository": False,
        },
    )

    blind_coverage = coverage(blind_labels)
    blind_label_hash = sha256_file(external_blind_labels)
    write_json(
        data_root / "sealed" / "frozen_blind.labels.jsonl",
        {
            "schema_version": "trip-nlu-v2-blind-label-seal-v1",
            "scoring_payload_present": False,
            "case_count": 24,
            "case_id_start": "TRIP_NLU_0097",
            "case_id_end": "TRIP_NLU_0120",
            "external_label_sha256": blind_label_hash,
            "coverage": blind_coverage,
        },
    )
    stale_receipt = data_root / "sealed" / "frozen_blind.validation_receipt.json"
    if stale_receipt.exists():
        stale_receipt.unlink()
    total_coverage = {
        "dev": coverage(all_cases["dev"]),
        "validation": coverage(all_cases["validation"]),
        "frozen_blind": blind_coverage,
        "total": coverage([*all_cases["dev"], *all_cases["validation"], *blind_labels]),
    }
    write_json(data_root / "coverage_report.json", total_coverage)
    tracked_files = [
        "README.md",
        "dev.jsonl",
        "validation.jsonl",
        "frozen_blind.inputs.jsonl",
        "source_registry.jsonl",
        "family_registry.jsonl",
        "generator_registry.json",
        "coverage_report.json",
        "sealed/frozen_blind.labels.jsonl",
    ]
    write_json(
        data_root / "manifest.json",
        {
            "schema_version": "trip-nlu-v2-manifest-v1",
            "case_id_start": "TRIP_NLU_0001",
            "case_id_end": "TRIP_NLU_0120",
            "case_count": 120,
            "blind_truth_in_repository": False,
            "proof_scope": "TEXT_REQUIREMENT_EXTRACTION_ONLY",
            "non_proof_scopes": ["OCR_ACCURACY", "PROVIDER_FACTS", "TRIP_REASONABLENESS", "HUMAN_EVIDENCE"],
            "code_bindings": {
                "schema_sha256": hashlib.sha256(
                    json.dumps(
                        TripIntakeExtraction.model_json_schema(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "validator_sha256": sha256_file(ROOT / "evals" / "trip_nlu_v2" / "validator.py"),
                "scorer_sha256": sha256_file(ROOT / "evals" / "trip_nlu_v2" / "scorer.py"),
                "gate_sha256": sha256_file(ROOT / "evals" / "trip_nlu_v2" / "gate.py"),
                "generator_sha256": sha256_file(Path(__file__)),
            },
            "files": {name: sha256_file(data_root / name) for name in tracked_files},
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DATA_ROOT)
    parser.add_argument("--external-blind-labels", type=Path, required=True)
    parser.add_argument("--user-prompt", type=Path, required=True)
    args = parser.parse_args()
    generate(
        args.output.resolve(),
        args.external_blind_labels.resolve(strict=True),
        args.user_prompt.resolve(strict=True),
    )


if __name__ == "__main__":
    main()
