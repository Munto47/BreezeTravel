"""Deterministic intent constraints for everyday place discovery.

LLMs may expand queries, but explicit category/entity/location intent is a hard
contract.  This module keeps that contract consistent across routing, Amap,
SSE previews, synthesis and critic checks.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.schemas.place import PlaceCategory
from app.constraints.city_knowledge import airport_layover_destinations


_FOOD_RE = re.compile(
    r"美食|吃的|吃饭|用餐|餐饮|餐厅|饭店|饭馆|早餐|午餐|午饭|晚餐|晚饭|正餐|夜宵|小吃|火锅|烧烤|"
    r"咖啡|菜馆|料理|素食|蔬食|素菜|素斋|烤鸭|生煎|小笼|豆汁|卤煮|片儿川|"
    r"本帮菜|上海菜|杭帮菜|杭州菜|北京菜|川菜|湘菜|粤菜|清真|面馆|一人食|"
    r"小馆|馆子|吃顿|吃点|吃些|吃居民|甜品|甜点|喝茶|茶馆"
)
_HOTEL_RE = re.compile(
    r"酒店|住宿|民宿|旅馆|客栈|宾馆|住一晚|住哪里|落脚|过夜|入住|"
    r"想住|要住|每晚|客房|房型|"
    r"住.{0,10}(?:附近|周边|哪儿|哪里|家庭房|客房|房型|洗衣|厨房)"
)
_ATTRACTION_RE = re.compile(
    r"景点|景区|好玩|有趣的地方|玩的地方|玩半天|游玩|散步|公园|博物馆|科技馆|美术馆|场馆|古镇|"
    r"乐园|动物园|园林|外滩|高楼|地标|看展|展览|艺术|人文|历史|课本|建筑|城市风貌|城市景观|城市变迁|夜景|街区|胡同|"
    r"里弄|运河|西湖|灵隐寺|天坛|故宫|景山|豫园|看看|看点东西|逛逛|参观|拍照|摄影|日出|天际线|"
    r"秋色|秋景|红叶|梧桐|老社区|出去走走|看一眼|"
    r"逛(?:有|点|些|老|街|胡同|里弄|社区)[^，。；！？,;!?]{0,16}(?:地方|空间|社区|街区)?|"
    r"轮椅.{0,12}(?:经典|景观|地方)|能逛"
)
_INDEPENDENT_ATTRACTION_RE = re.compile(
    r"美景|必去地标|景点|景区|好玩|玩的地方|玩一天|玩半天|游玩|散步|公园|博物馆|科技馆|美术馆|场馆|"
    r"看展|展览|艺术|人文|历史|夜景|街区|老社区|胡同|里弄|运河|参观|看看|看点东西|出去走走|"
    r"先去|再去|逛(?!完)"
)
_TRANSPORT_RE = re.compile(r"地铁站|公交站|机场|火车站|高铁站|码头|渡口")
_NEGATION_BEFORE_RE = re.compile(
    r"(?:不只想看|不只看|不只是|不想|不要|不去|不必|不用|不一定|别去|别给|排除|避开)"
    r"[^，。；！？,;!?]{0,10}$"
)
_BUDGET_RE = re.compile(r"(?:人均|每晚|预算)?\s*(\d{2,5}|[一二两三四五六七八九十百千]+)\s*(?:元|块|以内|以下|上下|左右|多)")

_CATEGORY_LABELS = {
    PlaceCategory.FOOD: "美食",
    PlaceCategory.ATTRACTION: "景点",
    PlaceCategory.HOTEL: "住宿",
    PlaceCategory.TRANSPORT: "交通",
}

# Triggers are intentionally product-scoped rather than a universal gazetteer.
# They resolve the high-value Shanghai phrases in the daily-query suite while
# remaining easy to extend from real failure cases.
_LANDMARKS: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = (
    (("故宫博物院", "故宫"), "故宫博物院", ("故宫博物院", "故宫")),
    (("景山公园", "景山"), "景山公园", ("景山公园", "景山")),
    (("天坛公园", "天坛"), "天坛公园", ("天坛公园", "天坛")),
    (("颐和园",), "颐和园", ("颐和园",)),
    (("香山公园", "香山"), "香山公园", ("香山公园", "香山")),
    (("地坛公园", "地坛"), "地坛公园", ("地坛公园", "地坛")),
    (("钟鼓楼", "北京鼓楼", "鼓楼"), "北京钟鼓楼", ("北京钟鼓楼", "钟鼓楼", "鼓楼")),
    (("永定门公园", "永定门"), "永定门公园", ("永定门公园", "永定门")),
    (("798艺术区", "798"), "798艺术区", ("798艺术区", "798")),
    (("UCCA尤伦斯当代艺术中心", "UCCA尤伦斯", "UCCA"), "UCCA尤伦斯当代艺术中心", ("UCCA尤伦斯当代艺术中心", "UCCA尤伦斯", "UCCA")),
    (("红砖美术馆",), "红砖美术馆", ("红砖美术馆",)),
    (("八达岭长城", "慕田峪长城", "长城"), "长城", ("八达岭长城", "长城（八达岭）", "慕田峪长城", "长城")),
    (("北京奥林匹克公园", "奥林匹克公园", "奥运公园"), "北京奥林匹克公园", ("北京奥林匹克公园", "奥林匹克公园中心区", "奥运公园")),
    (("中国第一高楼", "中国最高楼", "上海中心大厦", "上海中心", "上海之巅观光厅", "上海之巅"), "上海中心大厦", ("上海中心大厦", "上海中心", "上海之巅观光厅", "上海之巅")),
    (("上海迪士尼乐园", "迪士尼乐园", "迪士尼"), "上海迪士尼乐园", ("上海迪士尼乐园", "上海迪士尼度假区", "迪士尼乐园")),
    (("上海野生动物园", "野生动物园"), "上海野生动物园", ("上海野生动物园", "野生动物园")),
    (("上海外滩", "外滩"), "外滩", ("外滩",)),
    (("豫园",), "豫园", ("豫园",)),
    (("武康路历史文化名街", "武康路"), "武康路历史文化名街", ("武康路历史文化名街", "武康路")),
    (("思南路",), "思南路", ("思南路",)),
    (("愚园路",), "愚园路", ("愚园路",)),
    (("上海博物馆",), "上海博物馆", ("上海博物馆",)),
    (("西岸美术馆",), "西岸美术馆", ("西岸美术馆",)),
    (("龙美术馆",), "龙美术馆", ("龙美术馆",)),
    (("浦东美术馆",), "浦东美术馆", ("浦东美术馆",)),
    (("灵隐寺", "灵隐"), "灵隐寺", ("灵隐寺", "灵隐")),
    (("杭州西湖", "西湖"), "杭州西湖风景名胜区", ("杭州西湖风景名胜区", "西湖")),
    (("浙江省博物馆",), "浙江省博物馆", ("浙江省博物馆",)),
    (("浙江美术馆",), "浙江美术馆", ("浙江美术馆",)),
    (("中国美术学院美术馆", "中国美术学院南山校区美术馆"), "中国美术学院美术馆", ("中国美术学院美术馆", "中国美术学院南山校区美术馆")),
    (("天目里",), "天目里", ("天目里",)),
    (("北山街",), "北山街历史文化街区", ("北山街历史文化街区", "北山街")),
    (("集贤亭",), "杭州西湖风景名胜区-集贤亭", ("杭州西湖风景名胜区-集贤亭", "集贤亭")),
    (("九溪烟树", "九溪"), "九溪烟树", ("九溪烟树", "九溪")),
    (("太子湾公园", "太子湾"), "太子湾公园", ("太子湾公园", "太子湾")),
    (("柳浪闻莺",), "杭州西湖风景名胜区-柳浪闻莺", ("杭州西湖风景名胜区-柳浪闻莺", "柳浪闻莺")),
    (("断桥残雪",), "杭州西湖风景名胜区-断桥残雪", ("杭州西湖风景名胜区-断桥残雪", "断桥残雪")),
    (("京杭大运河", "运河"), "京杭大运河杭州景区", ("京杭大运河", "运河")),
    (("最繁荣的地方", "最繁华的地方", "最繁荣", "最繁华", "南京路步行街", "南京东路"), "南京路步行街", ("南京路步行街", "南京东路", "陆家嘴")),
)


def infer_requested_categories(text: str, explicit_category: str = "") -> set[PlaceCategory]:
    value = f"{text or ''} {explicit_category or ''}"
    categories: set[PlaceCategory] = set()
    if _has_positive_match(_FOOD_RE, value):
        categories.add(PlaceCategory.FOOD)
    if _has_positive_match(_HOTEL_RE, value):
        categories.add(PlaceCategory.HOTEL)
    if _has_positive_match(_ATTRACTION_RE, value):
        categories.add(PlaceCategory.ATTRACTION)
    # "住牛街附近，想吃..." describes the current base.  It must not create
    # a hotel recommendation slot merely because the location phrase contains
    # 住...附近.
    explicit_lodging = re.search(
        r"酒店|住宿|民宿|旅馆|客栈|宾馆|入住|过夜|住一晚|住哪里|住哪儿|想住|要住",
        value,
    )
    locative_stay = re.search(
        r"(?:^|[，。；,;])(?:我|我们|一家人)?住[^，。；,;]{1,16}(?:附近|周边)(?:[，。；,;]|$)",
        value,
    )
    if PlaceCategory.HOTEL in categories and locative_stay and not explicit_lodging:
        categories.discard(PlaceCategory.HOTEL)
    if (
        PlaceCategory.HOTEL in categories
        and "回酒店" in value
        and not re.search(r"想住|要住|住宿|入住|酒店.{0,8}(?:推荐|候选|选择)", value)
    ):
        categories.discard(PlaceCategory.HOTEL)
    if (
        PlaceCategory.HOTEL not in categories
        and PlaceCategory.FOOD not in categories
        and re.search(r"出差.{0,12}(?:三天|几天|预算)", value)
        and "预算" in value
        and re.search(r"步行|地铁.{0,6}(?:公司|会场)", value)
    ):
        categories.add(PlaceCategory.HOTEL)
    # In a lodging request, "吃饭方便" describes the desired surroundings; it
    # is not a second request for restaurant cards.
    if (
        PlaceCategory.HOTEL in categories
        and PlaceCategory.FOOD in categories
        and re.search(r"(?:吃饭|用餐).{0,4}(?:方便|便利)", value)
        and not re.search(r"(?:再|然后|之后|最后|顺便).{0,8}(?:吃饭|用餐|餐厅)", value)
    ):
        categories.discard(PlaceCategory.FOOD)
    if (
        PlaceCategory.ATTRACTION in categories
        and categories & {PlaceCategory.FOOD, PlaceCategory.HOTEL}
        and not _has_positive_match(_INDEPENDENT_ATTRACTION_RE, value)
    ):
        categories.discard(PlaceCategory.ATTRACTION)
    if (
        PlaceCategory.HOTEL in categories
        and PlaceCategory.ATTRACTION in categories
        and re.search(r"(?:想住|要住|住).{0,24}(?:氛围|建筑感|气质).{0,10}(?:酒店|住宿)", value)
        and not re.search(r"(?:先|再|然后|顺便)(?:去|看|逛|玩)|(?:去|看|逛|玩).{0,8}(?:之后|以后)", value)
    ):
        categories.discard(PlaceCategory.ATTRACTION)
    # An airport is usually a location anchor in a hotel/food request.  Only
    # treat transport as the requested result when the user asks for a station.
    if _TRANSPORT_RE.search(value) and re.search(r"怎么去|哪个站|交通|地铁怎么坐|公交怎么坐", value):
        categories.add(PlaceCategory.TRANSPORT)
    if not categories and extract_landmark_groups(value):
        categories.add(PlaceCategory.ATTRACTION)
    return categories


def _has_positive_match(pattern: re.Pattern[str], value: str) -> bool:
    for match in pattern.finditer(value):
        prefix = value[max(0, match.start() - 16):match.start()]
        if not _NEGATION_BEFORE_RE.search(prefix):
            return True
    return False


def _first_positive_term(value: str, terms: tuple[str, ...]) -> str:
    for term in terms:
        start = value.find(term)
        if start < 0:
            continue
        prefix = value[max(0, start - 16):start]
        if not _NEGATION_BEFORE_RE.search(prefix):
            return term
    return ""


def _last_positive_term(value: str, terms: tuple[str, ...]) -> str:
    """Return the last non-negated anchor mentioned by the visitor."""
    hits: list[tuple[int, str]] = []
    for term in terms:
        start = value.rfind(term)
        if start < 0:
            continue
        prefix = value[max(0, start - 16):start]
        if not _NEGATION_BEFORE_RE.search(prefix):
            hits.append((start, term))
    return max(hits, default=(-1, ""))[1]


def requested_category_argument(text: str) -> str:
    categories = infer_requested_categories(text)
    if len(categories) != 1:
        return ""
    return _CATEGORY_LABELS[next(iter(categories))]


def extract_landmark_groups(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Return canonical landmark queries in the order expressed by the user."""
    value = text or ""
    hits: list[tuple[int, str, tuple[str, ...]]] = []
    for triggers, canonical, aliases in _LANDMARKS:
        positions = []
        for trigger in triggers:
            start = value.find(trigger)
            if start < 0:
                continue
            if trigger == "西湖" and value[start:start + len(trigger) + 1] == "西湖区":
                continue
            prefix = value[max(0, start - 16):start]
            if not _NEGATION_BEFORE_RE.search(prefix):
                positions.append(start)
        if positions:
            hits.append((min(position for position in positions if position >= 0), canonical, aliases))
    hits.sort(key=lambda item: item[0])
    return [(canonical, aliases) for _, canonical, aliases in hits]


def is_closed_landmark_request(text: str, groups: list[tuple[str, tuple[str, ...]]]) -> bool:
    """Whether named landmarks are the complete requested destination list."""
    value = text or ""
    compact_value = "".join(value.lower().split())
    if len(groups) == 1:
        canonical, aliases = groups[0]
        exact_names = {"".join(item.lower().split()) for item in (canonical, *aliases)}
        if compact_value in exact_names:
            return True
    if len(groups) >= 2 and any(term in value for term in ("先", "再", "然后", "最后", "顺序", "按这个")):
        return True
    if len(groups) >= 2 and not any(term in value for term in ("搭配", "再推荐", "还有", "附近", "周边")):
        return True
    if (
        len(groups) == 1
        and any(term in value for term in ("附近", "周边", "就近"))
        and any(term in value for term in ("吃", "餐厅", "饭", "咖啡"))
    ):
        return True
    return bool(
        len(groups) == 1
        and any(term in value for term in ("我要去", "想去", "就去", "只去", "那里", "这个地方"))
    )


def _expanded_attraction_queries(value: str) -> list[str]:
    """Return independent, bounded POI queries for recurring open intents."""

    if "转机" in value:
        city = _first_positive_term(value, ("北京", "上海", "杭州"))
        airport = _first_positive_term(value, ("首都机场", "虹桥机场", "浦东机场", "萧山机场"))
        layover = airport_layover_destinations(city, airport)
        if layover:
            return layover

    if any(term in value for term in ("动物和自然", "动物与自然", "喜欢动物", "自然探索")):
        if "北京" in value:
            return ["北京动物园", "国家自然博物馆", "北京海洋馆"]
        if "上海" in value:
            return ["上海自然博物馆", "上海动物园", "上海海昌海洋公园"]
        if "杭州" in value:
            return ["杭州动物园", "浙江自然博物院杭州馆", "中国湿地博物馆"]

    if any(term in value for term in ("明清史", "明清历史")) and "北京" in value:
        return ["故宫博物院", "中国国家博物馆", "恭王府博物馆"]
    if any(term in value for term in ("近代史", "城市变迁", "历史建筑")) and "上海" in value:
        return ["上海市历史博物馆", "上海城市规划展示馆", "外滩"]
    if any(term in value for term in ("南宋史", "南宋历史")) and "杭州" in value:
        return ["南宋官窑博物馆", "杭州博物馆", "南宋御街"]

    if any(term in value for term in ("科技", "机器人", "自然科学", "科普")):
        if "北京" in value:
            return ["中国科学技术馆", "北京科学中心", "中国地质博物馆"]
        if "上海" in value:
            return ["上海科技馆", "上海自然博物馆", "上海天文馆"]
        if "杭州" in value:
            return ["浙江自然博物院杭州馆", "杭州低碳科技馆", "浙江省科技馆"]

    # Weather/indoor is a hard place-shape constraint.  Put it after the more
    # specific science/nature themes so "雨天科普" keeps its subject.
    if any(term in value for term in ("下雨", "雨天", "大雨", "室内场馆", "室内文化")):
        if "北京" in value:
            return ["中国国家博物馆", "北京市规划展览馆", "首都博物馆"]
        if "上海" in value:
            return ["上海自然博物馆", "上海科技馆", "上海博物馆"]
        if "杭州" in value:
            return ["浙江自然博物院杭州馆", "浙江省科技馆", "中国京杭大运河博物馆"]

    if any(term in value for term in ("亲子景点", "两个有趣", "好玩的地方", "玩的地方")):
        if "北京" in value:
            if "朝阳区" in value:
                return ["中国科学技术馆", "中国电影博物馆", "中国铁道博物馆东郊展馆"]
            return ["中国科学技术馆", "北京科学中心", "国家自然博物馆"]
        if "上海" in value:
            if "浦东新区" in value or "浦东" in value:
                return ["上海科技馆", "上海海洋水族馆", "上海天文馆"]
            return ["上海自然博物馆", "上海科技馆", "上海天文馆"]
        if "杭州" in value:
            if "西湖区" in value:
                return ["杭州动物园", "中国湿地博物馆", "浙江大学艺术与考古博物馆"]
            return ["浙江自然博物院杭州馆", "杭州低碳科技馆", "杭州动物园"]

    if any(term in value for term in ("学生预算", "免费或便宜", "免费", "低预算")):
        if "北京" in value and "海淀" in value:
            return ["海淀公园", "紫竹院公园", "国家图书馆"]
        if "杭州" in value and "西湖区" in value:
            return ["杭州西湖风景名胜区", "中国湿地博物馆", "浙江省博物馆之江馆"]

    if "夜景" in value and any(term in value for term in ("散步", "一个人", "独自", "不偏", "安全")):
        if "北京" in value:
            return ["什刹海", "前门大街", "北京奥林匹克公园"]
        if "上海" in value:
            return ["外滩", "北外滩滨江", "陆家嘴滨江"]
        if "杭州" in value:
            return ["杭州湖滨步行街", "京杭大运河杭州景区", "钱江新城城市阳台"]

    if "夜景" in value:
        if "北京" in value:
            return ["什刹海", "前门大街", "北京奥林匹克公园"]
        if "上海" in value:
            return ["外滩", "北外滩滨江", "陆家嘴滨江"]
        if "杭州" in value:
            return ["杭州湖滨步行街", "钱江新城城市阳台", "京杭大运河杭州景区"]

    if any(term in value for term in ("日出", "摄影", "拍照")):
        if "北京" in value:
            return ["景山公园", "钟鼓楼", "永定门公园"]
        if "上海" in value:
            return ["外滩", "北外滩滨江", "徐汇滨江"]
        if "杭州" in value:
            return ["杭州西湖风景名胜区", "北山街", "集贤亭"]
    if any(term in value for term in ("胡同", "里弄", "梧桐", "老社区", "生活气")):
        if "北京" in value:
            return ["史家胡同", "东交民巷", "砖塔胡同"]
        if "上海" in value:
            return ["武康路历史文化名街", "思南露天博物馆", "愚园路历史名人墙"]
        if "杭州" in value:
            if "运河" in value:
                return ["京杭大运河杭州景区", "小河直街", "大兜路历史文化街区"]
            return ["小河直街", "大兜路历史文化街区", "桥西历史文化街区"]
    if any(term in value for term in ("轮椅", "无障碍")):
        if "北京" in value:
            return ["故宫博物院", "天坛公园", "什刹海"]
        if "上海" in value:
            return ["外滩", "上海博物馆", "陆家嘴滨江"]
        if "杭州" in value:
            return ["西湖", "浙江省博物馆", "京杭大运河博物馆"]
    if any(term in value for term in ("老人", "七十", "七十五", "走不了", "少步行", "腿脚")):
        if "北京" in value:
            if "皇家园林" in value:
                return ["北海公园", "中山公园", "颐和园"]
            return ["北海公园", "天坛公园", "中国国家博物馆"]
        if "上海" in value:
            return ["外滩", "上海博物馆", "陆家嘴滨江"]
        if "杭州" in value:
            return ["杭州西湖风景名胜区", "浙江省博物馆", "柳浪闻莺"]
    if any(term in value for term in ("当代艺术", "看展", "艺术空间", "工业改造")):
        if "北京" in value:
            return ["798艺术区", "UCCA尤伦斯当代艺术中心", "红砖美术馆"]
        if "上海" in value:
            return ["西岸美术馆", "龙美术馆", "浦东美术馆"]
        if "杭州" in value:
            return ["浙江美术馆", "中国美术学院美术馆", "天目里"]
    if any(term in value for term in ("秋色", "秋景", "梧桐", "红叶")):
        if "北京" in value:
            return ["颐和园", "香山公园", "地坛公园"]
        if "上海" in value:
            return ["武康路", "衡山路", "上海植物园"]
        if "杭州" in value:
            return ["北山街", "九溪烟树", "太子湾公园"]
    if any(term in value for term in ("第一次", "首次", "代表性", "经典地标")):
        if "北京" in value:
            return ["故宫博物院", "景山公园", "天坛公园"]
        if "上海" in value:
            return ["外滩", "上海博物馆", "上海中心大厦"]
        if "杭州" in value:
            return ["杭州西湖风景名胜区", "灵隐寺", "京杭大运河杭州景区"]
    return []


def _representative_complements(value: str, landmark_names: list[str]) -> list[str]:
    """Bound relevant complements for a named landmark plus open discovery."""
    if not landmark_names:
        return []
    if any(term in value for term in ("日出", "摄影", "拍照")):
        city_options = {
            "北京": ["景山公园", "钟鼓楼", "永定门公园"],
            "上海": ["外滩", "北外滩滨江", "徐汇滨江"],
            "杭州": ["杭州西湖风景名胜区", "北山街", "集贤亭"],
        }
    elif (
        any(term in value for term in ("老人", "七十", "七十五", "走不了", "少步行", "腿脚"))
        and not any(term in value for term in ("吃", "餐厅", "用餐", "饭"))
    ):
        city_options = {
            "北京": ["北海公园", "中山公园", "颐和园"],
            "上海": ["外滩", "上海博物馆", "陆家嘴滨江"],
            "杭州": ["杭州西湖风景名胜区", "柳浪闻莺", "曲院风荷"],
        }
    elif not any(term in value for term in ("第一次", "首次")):
        return []
    else:
        if not any(term in value for term in ("味道", "代表性", "经典", "老城", "人文")):
            return []
        city_options = {
            "北京": ["景山公园", "天坛公园", "中国国家博物馆"],
            "上海": ["上海博物馆", "豫园", "武康路历史文化名街"],
            "杭州": ["灵隐寺", "京杭大运河杭州景区", "杭州博物馆"],
        }
    city = next((name for name in city_options if name in value), "")
    compact_landmarks = {"".join(name.lower().split()) for name in landmark_names}
    return [
        option for option in city_options.get(city, [])
        if "".join(option.lower().split()) not in compact_landmarks
    ][:2]


def build_place_search_queries(text: str, city: str = "") -> list[str]:
    """Create bounded, intent-preserving Amap searches for a user utterance."""
    # Room metadata is the authoritative city even when the visitor naturally
    # says only "西湖" or "外滩" in the message.
    value = f"{city or ''} {text or ''}".strip()
    categories = infer_requested_categories(value)
    landmark_groups = extract_landmark_groups(value)
    expanded_attractions = _expanded_attraction_queries(value)
    if categories == {PlaceCategory.ATTRACTION} and expanded_attractions:
        return expanded_attractions

    # Free-form provider keywords such as "摄影地点" and "城市漫步" often
    # retrieve camera shops or return nothing.  Use real destination anchors
    # for these recurring visitor intents, while still letting the downstream
    # critic reject unsuitable POIs.
    if categories == {PlaceCategory.ATTRACTION} and any(term in value for term in ("日出", "摄影", "拍照", "天际线")):
        if "北京" in value:
            return ["景山公园", "钟鼓楼", "永定门公园"]
        if "上海" in value:
            return ["外滩", "北外滩滨江", "徐汇滨江"]
        if "杭州" in value:
            return ["杭州西湖风景名胜区", "北山街", "集贤亭"]
    if categories == {PlaceCategory.ATTRACTION} and any(term in value for term in ("胡同", "里弄", "梧桐", "老社区", "生活气")):
        if "北京" in value:
            return ["史家胡同", "五道营胡同", "东交民巷"]
        if "上海" in value:
            return ["武康路历史文化名街", "思南露天博物馆", "愚园路历史名人墙"]
        if "杭州" in value:
            if "运河" in value:
                return ["京杭大运河杭州景区", "小河直街", "大兜路历史文化街区"]
            return ["小河直街", "大兜路历史文化街区", "桥西历史文化街区"]

    if categories == {PlaceCategory.ATTRACTION} and any(term in value for term in ("轮椅", "无障碍")):
        if "北京" in value:
            return ["故宫博物院", "天坛公园", "什刹海"]
        if "上海" in value:
            return ["外滩", "上海博物馆", "陆家嘴滨江"]
        if "杭州" in value:
            return ["西湖", "浙江省博物馆", "京杭大运河博物馆"]

    if categories == {PlaceCategory.ATTRACTION} and any(term in value for term in ("老人", "七十", "七十五", "走不了", "少步行", "腿脚")):
        if "北京" in value:
            return ["北海公园", "天坛公园", "中国国家博物馆"]
        if "上海" in value:
            return ["外滩", "上海博物馆", "陆家嘴滨江"]
        if "杭州" in value:
            return ["杭州西湖风景名胜区", "浙江省博物馆", "柳浪闻莺"]

    if categories == {PlaceCategory.ATTRACTION} and any(term in value for term in ("当代艺术", "看展", "艺术空间", "工业改造")):
        if "北京" in value:
            return ["798艺术区", "UCCA尤伦斯当代艺术中心", "红砖美术馆"]
        if "上海" in value:
            return ["西岸美术馆", "龙美术馆", "浦东美术馆"]
        if "杭州" in value:
            return ["浙江美术馆", "中国美术学院美术馆", "天目里"]

    if categories == {PlaceCategory.ATTRACTION} and any(term in value for term in ("秋色", "秋景", "梧桐", "红叶")):
        if "北京" in value:
            return ["颐和园", "香山公园", "地坛公园"]
        if "上海" in value:
            return ["武康路", "衡山路", "上海植物园"]
        if "杭州" in value:
            return ["北山街", "九溪烟树", "太子湾公园"]

    if categories == {PlaceCategory.ATTRACTION} and any(term in value for term in ("第一次", "首次", "代表性", "经典地标")):
        if "北京" in value:
            return ["故宫博物院", "天坛公园", "颐和园"]
        if "上海" in value:
            return ["外滩", "上海博物馆", "上海中心大厦"]
        if "杭州" in value:
            return ["杭州西湖风景名胜区", "灵隐寺", "京杭大运河杭州景区"]

    # A room-opening prompt often asks for attractions, food and hotels in one
    # long paragraph.  Passing that paragraph straight to search_places both
    # exceeds the runtime's bounded query contract and produces a poor POI
    # keyword.  Split it into one concise provider query per requested class.
    if len(categories) > 1:
        plan = build_category_search_plan(text, city, categories)
        return [query for category in (
            PlaceCategory.ATTRACTION, PlaceCategory.FOOD,
            PlaceCategory.HOTEL, PlaceCategory.TRANSPORT,
        ) for query in plan.get(category, [])]

    # Named landmarks are destinations only for attraction discovery.  In
    # "迪士尼附近吃饭", Disney is a geographic anchor, not a result card.
    if (
        landmark_groups
        and categories == {PlaceCategory.ATTRACTION}
        and is_closed_landmark_request(value, landmark_groups)
    ):
        return [canonical for canonical, _ in landmark_groups]

    if (
        PlaceCategory.FOOD in categories
        and "浦东新区" in value
        and any(term in value for term in ("南边", "南部", "海边", "临港", "南汇", "滴水湖"))
    ):
        return ["滴水湖附近餐厅", "南汇新城餐厅", "芦潮港海鲜餐厅"]
    if PlaceCategory.FOOD in categories and "七宝" in value:
        return ["七宝 早餐店" if "早餐" in value else "七宝 餐厅"]
    if PlaceCategory.FOOD in categories and any(
        term in value for term in ("素食", "蛋奶素", "蔬食", "素菜", "素斋")
    ):
        return ["素食餐厅", "蔬食餐厅", "素菜馆"]
    if PlaceCategory.HOTEL in categories and "虹桥机场" in value:
        return ["虹桥机场T2附近酒店", "虹桥枢纽酒店", "虹桥机场酒店"]
    if PlaceCategory.FOOD in categories and "迪士尼" in value and "附近" in value:
        return ["迪士尼小镇餐厅", "上海迪士尼度假区餐厅", "比斯特上海购物村餐厅"]
    if PlaceCategory.ATTRACTION in categories and "不要商场" in value and "公园" in value:
        return ["闵行区 公园 绿地"]
    if categories == {PlaceCategory.ATTRACTION}:
        return [_category_search_query(value, PlaceCategory.ATTRACTION)]
    if categories == {PlaceCategory.HOTEL}:
        return [_category_search_query(value, PlaceCategory.HOTEL)]
    if categories == {PlaceCategory.FOOD}:
        if "川菜" in value:
            return ["川菜馆", "四川菜", "麻辣餐厅"]
        return [_category_search_query(value, PlaceCategory.FOOD)]
    return [value]


def build_category_search_plan(
    text: str,
    city: str = "",
    categories: set[PlaceCategory] | None = None,
) -> dict[PlaceCategory, list[str]]:
    """Map each requested category to its own provider queries.

    Keeping the category alongside the keyword lets the Router repair a
    missing category deterministically after the first parallel search.
    """

    value = f"{city or ''} {text or ''}".strip()
    requested = categories if categories is not None else infer_requested_categories(value)
    landmark_groups = extract_landmark_groups(value)
    plan: dict[PlaceCategory, list[str]] = {}
    if PlaceCategory.ATTRACTION in requested:
        if landmark_groups:
            attraction_queries = [canonical for canonical, _ in landmark_groups]
        else:
            compound_scene = any(term in value for term in (
                "亲子景点", "两个有趣", "好玩的地方", "玩的地方", "下雨", "雨天", "大雨",
                "室内场馆", "室内文化", "明清史", "近代史", "南宋史", "老人", "少走路",
                "少爬坡", "老社区", "居民日常",
            ))
            expanded = (
                _expanded_attraction_queries(value)
                if len(requested) <= 2 or compound_scene else []
            )
            attraction_queries = expanded if len(expanded) > 1 else [
                _category_search_query(value, PlaceCategory.ATTRACTION)
            ]
        plan[PlaceCategory.ATTRACTION] = attraction_queries
    if PlaceCategory.FOOD in requested:
        expanded_food = _expanded_food_queries(value)
        plan[PlaceCategory.FOOD] = (
            expanded_food[:2] if len(requested) > 1 else expanded_food[:3]
        ) or [_category_search_query(value, PlaceCategory.FOOD)]
    if PlaceCategory.HOTEL in requested:
        plan[PlaceCategory.HOTEL] = [_category_search_query(value, PlaceCategory.HOTEL)]
    if PlaceCategory.TRANSPORT in requested:
        plan[PlaceCategory.TRANSPORT] = ["交通枢纽"]
    return plan


def _expanded_food_queries(value: str) -> list[str]:
    """Compile recurring dining constraints into short provider queries."""
    city = _first_positive_term(value, ("北京", "上海", "杭州"))
    if any(term in value for term in ("约客户", "商务午餐", "商务宴请")):
        return ["商务餐厅"]
    explicit_dishes = [
        term for term in ("烤鸭", "生煎", "小笼", "片儿川", "杭州面")
        if _first_positive_term(value, (term,))
    ]
    if explicit_dishes:
        return list(dict.fromkeys(explicit_dishes))
    # Explicit dishes/cuisines are stronger than the price/persona wording.
    # Keep them for the generic category compiler instead of replacing
    # "生煎、小笼" with the much broader "面馆、小吃快餐".
    if any(term in value for term in (
        "北京菜", "烤鸭", "清真", "北京小吃", "本帮菜", "上海菜", "上海小吃",
        "生煎", "小笼", "杭帮菜", "杭州菜", "杭州小吃", "片儿川", "杭州面",
        "素食", "蛋奶素",
    )):
        return []
    if any(term in value for term in ("大学生", "学生预算")) and any(
        term in value for term in ("人均", "预算", "以内")
    ):
        return ["面馆", "小吃快餐"]
    if "早餐" in value:
        return {
            "北京": ["豆汁", "炒肝", "卤煮"],
            "上海": ["生煎", "锅贴", "粢饭"],
            "杭州": ["片儿川", "杭州早餐", "小笼"],
        }.get(city, ["本地早餐", "早点", "面馆"])
    if any(term in value for term in ("一人食", "一个人", "小份")):
        return {
            "北京": ["北京小吃", "炸酱面", "卤煮"],
            "上海": ["本帮面馆", "生煎", "小笼"],
            "杭州": ["片儿川", "杭州面馆", "杭州小吃"],
        }.get(city, ["本地面馆", "小吃快餐"])
    if any(term in value for term in ("老社区", "居民日常", "居民常去", "生活气")):
        return {
            "北京": ["北京小吃", "京味家常菜"],
            "上海": ["本帮面馆", "上海家常菜"],
            "杭州": ["杭州面馆", "杭帮家常菜"],
        }.get(city, ["本地家常菜", "社区小馆"])
    if any(term in value for term in ("清淡", "老人", "少油", "少盐")):
        return ["家常菜", "粥店", "素菜馆"]
    if any(term in value for term in ("孩子友好", "适合孩子", "亲子餐厅", "带孩子", "一家人")):
        if "杭州" in value:
            return ["杭帮菜", "家常菜"]
        return ["家常菜", "亲子餐厅"]
    if any(term in value for term in ("夜宵", "十点半", "深夜")):
        return ["24小时餐厅", "夜宵", "火锅"]
    return []


def build_generic_category_query(
    text: str,
    city: str,
    category: PlaceCategory,
) -> str:
    """Compile one open-ended category query without closing on named entities."""
    return _category_search_query(f"{city or ''} {text or ''}".strip(), category)


def _category_search_query(value: str, category: PlaceCategory) -> str:
    """Turn human constraints into provider-friendly POI keywords."""
    if category == PlaceCategory.ATTRACTION:
        if "转机" in value:
            airport = _first_positive_term(value, ("首都机场", "虹桥机场", "浦东机场", "萧山机场"))
            if airport:
                return f"{airport}附近景点"
        if any(term in value for term in ("必去", "地标")):
            return "必去地标 景点"
        if any(term in value for term in ("科技", "机器人", "自然科学", "科普")):
            return "科技馆 自然博物馆"
        if any(term in value for term in ("下雨", "雨天", "室内场馆", "室内文化")):
            return "博物馆 美术馆"
        if any(term in value for term in ("当代艺术", "看展", "艺术", "设计")):
            return "美术馆 艺术中心"
        if any(term in value for term in ("日出", "摄影", "拍照", "天际线")):
            if "北京" in value:
                return "景山公园 鼓楼 观景"
            if "上海" in value:
                return "外滩 滨江 观景"
            if "杭州" in value:
                return "西湖 日出 观景"
        if any(term in value for term in ("胡同", "里弄", "梧桐", "老社区", "生活气")):
            return "历史文化街区 城市漫步"
        if any(term in value for term in ("夜景", "晚上", "夜间")):
            return "夜景 观景 公共空间"
        if any(term in value for term in ("建筑", "城市变迁")):
            return "历史建筑 博物馆"
        if any(term in value for term in ("公园", "散步", "绿地")):
            return "公园 绿地"
        if "运河" in value:
            return "京杭大运河 博物馆 历史街区"
        if any(term in value for term in ("经典", "代表性", "第一次", "地标", "无障碍")):
            return "热门经典景点"
        return "文化景点 博物馆"
    if category == PlaceCategory.HOTEL:
        if any(term in value for term in ("不同价位", "价位")):
            return "不同价位 酒店 民宿"
        anchors = _first_positive_term(value, (
            "首都机场", "北京南站", "国贸", "王府井", "故宫", "虹桥机场", "虹桥站", "迪士尼",
            "浦东机场", "外滩", "陆家嘴", "西湖", "萧山机场", "杭州东站", "钱江新城",
        ))
        if any(term in value for term in ("胡同氛围", "四合院", "北京胡同")):
            style = "四合院 酒店"
        elif "上海" in value and any(term in value for term in ("老上海", "建筑感", "历史建筑")):
            style = "历史建筑 酒店"
        elif "杭州" in value and any(term in value for term in ("杭州气质", "山居", "茶宿")):
            style = "客栈 民宿"
        elif any(term in value for term in ("精品酒店", "精品住宿")):
            style = "精品酒店"
        elif any(term in value for term in ("洗衣", "厨房", "长住", "住一周", "住十天")):
            style = "公寓式酒店 洗衣"
        elif any(term in value for term in ("宠物", "小狗", "带猫")):
            style = "宠物友好酒店 停车"
        elif any(term in value for term in ("家庭房", "带孩子", "四岁")):
            style = "亲子酒店 家庭房"
        elif any(term in value for term in ("轮椅", "无障碍")):
            style = "酒店 无障碍客房"
        elif any(term in value for term in ("学生", "三百", "低预算", "经济")):
            style = "经济型酒店 地铁"
        else:
            style = "酒店"
        if anchors and any(term in value for term in ("家庭房", "带孩子", "四岁")):
            return f"{anchors}附近亲子酒店"
        if anchors:
            return f"{anchors}附近 {style}"
        return f"{anchors} {style}".strip()
    if any(term in value for term in ("老字号", "高分", "性价比")):
        return "本地老字号 高分餐厅"
    cuisine_terms = [term for term in (
        "北京菜", "烤鸭", "清真", "老北京早餐", "素食", "蛋奶素", "川菜", "本帮菜", "生煎", "小笼",
        "湘菜", "上海小吃", "杭帮菜", "杭州菜", "片儿川", "杭州面", "杭州小吃", "早餐", "夜宵",
        "甜品", "咖啡", "茶馆",
    ) if _first_positive_term(value, (term,))]
    # For an ordered mixed route, the final named area is normally where the
    # user wants to eat ("先灵隐，再西湖，最后湖滨吃饭").
    anchors = _last_positive_term(value, (
        "王府井", "牛街", "国贸", "三里屯", "前门", "北京南站", "迪士尼", "人民广场", "南京西路",
        "静安", "城隍庙", "徐家汇", "武康路", "陆家嘴", "虹桥站", "灵隐寺", "湖滨", "武林广场",
        "黄龙", "河坊街", "滨江", "龙井村", "钱江新城", "杭州东站",
    ))
    cuisine_terms = [
        "素食" if term == "蛋奶素" else "杭帮菜" if term == "杭州菜" else term
        for term in cuisine_terms
    ]
    cuisine = " ".join(dict.fromkeys(cuisine_terms[:2])) or ("亲子餐厅" if "孩子" in value else "本地特色餐厅")
    return f"{anchors} {cuisine}".strip()


def filter_places_for_request(
    places: Iterable[Any],
    text: str,
    explicit_category: str | Iterable[PlaceCategory | str] = "",
) -> list[Any]:
    if isinstance(explicit_category, str):
        explicit_categories = {
            category for category, label in _CATEGORY_LABELS.items()
            if explicit_category and label in explicit_category
        }
    else:
        explicit_categories: set[PlaceCategory] = set()
        for raw_category in explicit_category:
            try:
                explicit_categories.add(
                    raw_category
                    if isinstance(raw_category, PlaceCategory)
                    else PlaceCategory(str(raw_category))
                )
            except ValueError:
                continue
    # A provider call already carries the category contract separately from
    # its location anchor. "天坛附近北京菜" + category=美食 must not retain
    # the attraction merely because the keyword contains 天坛.
    categories = explicit_categories or infer_requested_categories(text)
    items = list(places)
    if not categories:
        return items
    filtered = [
        place for place in items
        if getattr(place, "category", None) in categories
        or (
            isinstance(place, dict)
            and str(place.get("category") or "")
            in {category.value for category in categories}
        )
    ]
    negative_name_terms: list[str] = []
    if any(term in text for term in ("不要商场", "不想进商场", "不想逛商场", "排除商场")):
        negative_name_terms.extend((
            "商场", "购物中心", "商城", "大悦城", "王府井喜悦", "天街", "万象城",
            "环宇城", "日月光", "宝龙城", "南丰城", "百货", "购物广场", "购物城",
            "芮欧", "来福士", "国金中心",
        ))
    if any(term in text for term in ("不要肯德基", "不要肯德基、麦当劳", "不要肯德基、星巴克")):
        negative_name_terms.extend(("肯德基", "麦当劳", "星巴克"))
    if any(term in text for term in ("不要把咖啡店", "不要咖啡店")):
        negative_name_terms.extend(("咖啡", "Coffee", "coffee", "Café", "café"))
    if any(term in text for term in ("不去长城", "不要长城")):
        negative_name_terms.append("长城")
    if any(term in text for term in ("不去乐园", "排除主题乐园")):
        negative_name_terms.extend(("乐园", "主题公园"))
    if any(term in text for term in ("拍照", "摄影", "日出", "机位")):
        negative_name_terms.extend(("摄影器材", "照相馆", "写真馆", "婚纱摄影"))
    if negative_name_terms:
        filtered = [
            place for place in filtered
            if not any(
                term in (
                    f"{_raw_value(place, 'name', '')} "
                    f"{_raw_value(place, 'address', '')}"
                )
                for term in negative_name_terms
            )
        ]
    if any(term in text for term in ("全国连锁", "不要连锁", "别给我连锁", "排除连锁")):
        filtered = [
            place for place in filtered
            if "连锁品牌" not in " ".join(_raw_value(place, "tags", []) or [])
        ]
    groups = extract_landmark_groups(text)
    if groups and categories == {PlaceCategory.ATTRACTION} and is_closed_landmark_request(text, groups):
        landmark_matches: list[Any] = []
        for place in filtered:
            name = str(place.get("name", "") if isinstance(place, dict) else getattr(place, "name", ""))
            if any(any(alias in name for alias in aliases) for _, aliases in groups):
                landmark_matches.append(place)
        # A navigation-style request such as "长城然后奥林匹克公园" is a
        # request for those destinations, not five nearby POIs sharing a
        # keyword. Keep one provider-backed POI per explicit entity and keep
        # the user's order.
        ranked = rank_places_for_request(landmark_matches, text)
        selected: list[Any] = []
        for group_index in range(len(groups)):
            match = next((place for place in ranked if _landmark_group_index(place, groups) == group_index), None)
            if match is not None:
                selected.append(match)
        return selected
    if PlaceCategory.FOOD in categories and any(term in text for term in ("素食", "蛋奶素", "蔬食", "素菜", "素斋")):
        diet_terms = ("素食", "蔬食", "素菜", "素斋", "斋菜", "vegan", "vegetarian")
        filtered = [
            place for place in filtered
            if any(
                term.lower() in str(
                    place.get("name", "") if isinstance(place, dict) else getattr(place, "name", "")
                ).lower()
                for term in diet_terms
            )
        ]
    if PlaceCategory.FOOD in categories and any(term in text for term in ("清淡", "少油", "少盐")):
        heavy_terms = (
            "麻辣", "香辣", "川菜", "火锅", "烤肉", "烧烤", "大排档", "小龙虾",
            "东北菜", "傣家菜", "西北菜", "湘菜", "海鲜", "羊肉", "毛血旺",
        )
        mild = [
            place for place in filtered
            if not any(term in f"{_raw_value(place, 'name', '')} {' '.join(_raw_value(place, 'tags', []) or [])}" for term in heavy_terms)
        ]
        if sum(_raw_value(place, "category") == PlaceCategory.FOOD for place in mild) >= 1:
            filtered = mild
    if PlaceCategory.HOTEL in categories and any(term in text for term in ("胡同氛围", "四合院", "北京胡同")):
        style_terms = ("胡同", "四合院", "后海", "南锣鼓", "什刹海", "鼓楼")
        styled = [
            place for place in filtered
            if _raw_value(place, "category") != PlaceCategory.HOTEL
            or any(term in f"{_raw_value(place, 'name', '')} {_raw_value(place, 'address', '')}" for term in style_terms)
        ]
        if sum(_raw_value(place, "category") == PlaceCategory.HOTEL for place in styled) >= 2:
            filtered = styled
    budget = extract_budget_ceiling(text)
    if budget is not None:
        relevant = {PlaceCategory.FOOD, PlaceCategory.HOTEL} & categories
        affordable_or_unknown = [
            place for place in filtered
            if _raw_value(place, "category") not in relevant
            or not isinstance(_raw_value(place, "amap_price", None), (int, float))
            or float(_raw_value(place, "amap_price", 0)) <= budget
        ]
        affordable_count = sum(
            1 for place in affordable_or_unknown if _raw_value(place, "category") in relevant
        )
        if affordable_count >= 2:
            filtered = affordable_or_unknown
    if len(categories) >= 2 or "不重复分店" in text:
        deduplicated: list[Any] = []
        seen_brands: set[str] = set()
        for place in filtered:
            name = str(_raw_value(place, "name", ""))
            brand = re.split(r"[（(]", name, maxsplit=1)[0]
            brand = re.sub(r"(?:旗舰店|总店|分店|酒店|宾馆|旅馆)$", "", brand).strip().lower()
            if brand and brand in seen_brands:
                continue
            if brand:
                seen_brands.add(brand)
            deduplicated.append(place)
        filtered = deduplicated
    return filtered


def cluster_mixed_places_for_request(places: Iterable[Any], text: str) -> list[Any]:
    """Keep attraction+food recommendations in one practical district when asked."""
    items = list(places)
    categories = infer_requested_categories(text)
    if categories != {PlaceCategory.ATTRACTION, PlaceCategory.FOOD}:
        return items
    if not any(term in text for term in ("附近", "就近", "少换乘", "不跑远", "别跑远", "跨城")):
        return items
    by_district: dict[str, dict[PlaceCategory, list[Any]]] = {}
    for place in items:
        district = str(_raw_value(place, "district", "") or "")
        category = _raw_value(place, "category", None)
        if not district or category not in categories:
            continue
        by_district.setdefault(district, {PlaceCategory.ATTRACTION: [], PlaceCategory.FOOD: []})[category].append(place)
    viable = [
        (district, grouped) for district, grouped in by_district.items()
        if grouped[PlaceCategory.ATTRACTION] and grouped[PlaceCategory.FOOD]
    ]
    if not viable:
        return items
    _, grouped = max(
        viable,
        key=lambda entry: (
            min(len(entry[1][PlaceCategory.ATTRACTION]), len(entry[1][PlaceCategory.FOOD])),
            len(entry[1][PlaceCategory.ATTRACTION]) + len(entry[1][PlaceCategory.FOOD]),
        ),
    )
    return grouped[PlaceCategory.ATTRACTION][:3] + grouped[PlaceCategory.FOOD][:3]


def rank_places_for_request(places: Iterable[Any], text: str) -> list[Any]:
    """Prioritise explicitly named entities, then provider relevance/rating."""
    items = list(places)
    groups = extract_landmark_groups(text)

    budget = extract_budget_ceiling(text)

    semantic_terms = [
        term for term in (
            "小吃", "早餐", "夜宵", "生煎", "小笼", "豆汁", "炒肝", "卤煮",
            "炸酱面", "烤鸭", "涮肉", "火锅", "烧烤", "面馆", "家常菜",
            "咖啡", "茶馆", "甜品", "素食", "清真", "本帮菜", "杭帮菜",
            "北京菜", "川菜", "湘菜", "粤菜", "日料",
        )
        if term in (text or "")
    ]

    first_visit_multi_day = (
        any(term in text for term in ("第一次", "首次"))
        and any(term in text for term in ("三天", "四天", "多日"))
        and len(infer_requested_categories(text)) >= 3
    )
    core_district_tiers = {
        "北京": (("东城区", "西城区", "朝阳区", "海淀区"), ("丰台区", "石景山区")),
        "上海": (("黄浦区", "静安区", "徐汇区", "虹口区", "浦东新区", "长宁区"), ("普陀区", "杨浦区")),
        "杭州": (("上城区", "西湖区", "拱墅区"), ("滨江区",)),
    }

    def key(place: Any) -> tuple[int, int, int, int, int, int, int, float, float]:
        name = "".join(str(_raw_value(place, "name", "")).lower().split())
        group_index = len(groups)
        exactness = 1
        raw_category = _raw_value(place, "category", None)
        category_value = getattr(raw_category, "value", raw_category)
        # A landmark in an around-search request is a geographic anchor for
        # food/hotels, not an entity the restaurant name must repeat. Applying
        # landmark exactness to every category made generic "前门店" names beat
        # an explicit 北京小吃 match.
        if category_value == PlaceCategory.ATTRACTION.value:
            for index, (canonical, aliases) in enumerate(groups):
                compact_aliases = ["".join(alias.lower().split()) for alias in aliases]
                if any(alias in name for alias in compact_aliases):
                    group_index = index
                    exactness = 0 if name in compact_aliases else 1
                    break
        rating = _raw_value(place, "amap_rating", 0.0) or 0.0
        raw_name = str(_raw_value(place, "name", ""))
        raw_tags = " ".join(_raw_value(place, "tags", []) or [])
        haystack = f"{raw_name} {raw_tags}"
        # A POI whose identity is the requested subtype (护国寺小吃) is a
        # stronger match than a generic restaurant that merely lists a snack
        # platter in its menu tags. This stays data-driven and applies equally
        # to breakfast, cafes, cuisines and other explicit food semantics.
        semantic_rank = -sum(
            (3 if term in raw_name else 0) + (1 if term in raw_tags else 0)
            for term in semantic_terms
        )
        persona_rank = 0
        if any(term in text for term in ("一人食", "一个人", "小份")):
            persona_rank = 0 if any(term in haystack for term in ("面", "小吃", "快餐", "生煎", "小笼", "饺子", "馄饨")) else 1
        elif any(term in text for term in ("老社区", "居民日常", "居民常去")):
            persona_rank = 0 if any(term in haystack for term in ("家常", "面馆", "小吃", "饭店", "菜馆")) else 1
        semantic_area_rank = 0
        if "西湖边" in text:
            location_haystack = f"{raw_name} {_raw_value(place, 'address', '')} {_raw_value(place, 'district', '')}"
            direct_west_lake_terms = (
                "西湖", "湖滨", "南山路", "龙翔桥", "孤山", "北山", "东坡路", "学士路",
            )
            semantic_area_rank = (
                0 if any(term in location_haystack for term in direct_west_lake_terms)
                else 1 if _raw_value(place, "district", "") in {"西湖区", "上城区"}
                else 2
            )
        core_rank = 0
        if first_visit_multi_day:
            city = next((name for name in core_district_tiers if name in text), "")
            tiers = core_district_tiers.get(city, ())
            district = str(_raw_value(place, "district", "") or "")
            core_rank = next((index for index, tier in enumerate(tiers) if district in tier), len(tiers) + 1)
        price = _raw_value(place, "amap_price", None)
        if budget is None:
            budget_rank, price_distance = 0, 0.0
        elif isinstance(price, (int, float)) and price <= budget * 1.25:
            budget_rank, price_distance = 0, abs(float(price) - budget)
        elif price is None:
            budget_rank, price_distance = 1, budget
        else:
            budget_rank, price_distance = 2, float(price)
        return (
            group_index, exactness, semantic_rank, persona_rank,
            semantic_area_rank, core_rank, budget_rank, price_distance, -float(rating),
        )

    ranked = sorted(items, key=key)
    if not groups:
        return ranked

    # Reserve one best card for every explicitly requested entity before any
    # sub-attractions consume the per-category cap.
    selected: list[Any] = []
    selected_ids: set[int] = set()
    for group_index in range(len(groups)):
        for item in ranked:
            if id(item) in selected_ids:
                continue
            if key(item)[0] == group_index:
                selected.append(item)
                selected_ids.add(id(item))
                break
    # After reserving one exact card per named landmark, prefer complementary
    # destinations over several sub-POIs that repeat the same landmark name.
    selected.extend(
        item for item in ranked
        if id(item) not in selected_ids and _landmark_group_index(item, groups) == len(groups)
    )
    selected.extend(
        item for item in ranked
        if id(item) not in selected_ids and _landmark_group_index(item, groups) != len(groups)
    )
    return selected


def _raw_value(place: Any, key: str, default: Any = "") -> Any:
    return place.get(key, default) if isinstance(place, dict) else getattr(place, key, default)


def extract_budget_ceiling(text: str) -> float | None:
    match = _BUDGET_RE.search(text or "")
    if not match:
        return None
    raw = match.group(1)
    if raw.isdigit():
        return float(raw)
    common = {
        "六十": 60, "八十": 80, "一百": 100, "一百五十": 150, "三百": 300,
        "四百": 400, "五百": 500, "六百": 600, "七百": 700,
        "八百": 800, "九百": 900, "一千": 1000, "一千二": 1200,
    }
    return float(common[raw]) if raw in common else None


def _landmark_group_index(
    place: Any,
    groups: list[tuple[str, tuple[str, ...]]],
) -> int:
    """Return the explicit landmark group matched by a provider POI."""
    category = _raw_value(place, "category", None)
    category_value = getattr(category, "value", category)
    if category_value != PlaceCategory.ATTRACTION.value:
        return len(groups)
    name = str(place.get("name", "") if isinstance(place, dict) else getattr(place, "name", ""))
    compact_name = "".join(name.lower().split())
    for index, (_, aliases) in enumerate(groups):
        if any("".join(alias.lower().split()) in compact_name for alias in aliases):
            return index
    return len(groups)


def request_has_all_landmarks(places: Iterable[Any], text: str) -> bool:
    if PlaceCategory.ATTRACTION not in infer_requested_categories(text):
        return True
    groups = extract_landmark_groups(text)
    if not groups:
        return True
    names = [
        str(place.get("name", "") if isinstance(place, dict) else getattr(place, "name", ""))
        for place in places
        if getattr(_raw_value(place, "category", None), "value", _raw_value(place, "category", None))
        == PlaceCategory.ATTRACTION.value
    ]
    for _, aliases in groups:
        if not any(any(alias in name for alias in aliases) for name in names):
            return False
    return True
