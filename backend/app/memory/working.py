"""
Working Memory（工作记忆）

在单次对话中维护和更新用户的结构化偏好。
每次工具调用完成后，从最新对话消息中提取偏好更新到 WorkingContext。

设计原则
--------
- 轻量：尽量用规则提取，避免每次都调用 LLM（降低延迟）
- 累积：偏好只增不减（用户说了"不喜欢商业区"就永久记录到本次会话）
- 可读：格式化为自然语言文本注入 system prompt
"""

import re
from typing import Optional

from app.agents.state import WorkingContext, default_working_context


# 关键词映射表（规则提取）
_STYLE_KEYWORDS = {
    "亲子": ["孩子", "小孩", "带娃", "亲子", "儿童", "小朋友", "幼儿"],
    "情侣": ["情侣", "约会", "恋人", "男友", "女友", "蜜月", "两人世界"],
    "独行": ["一个人", "独自", "单独", "背包客", "独行", "Solo"],
    "闺蜜": ["闺蜜", "姐妹", "好友", "朋友们", "一群女生"],
    "商务": ["出差", "商务", "会议", "公司"],
    "家庭": ["全家", "父母", "老人", "长辈", "爸妈"],
}

_BUDGET_KEYWORDS = {
    "高": ["不差钱", "高端", "奢华", "豪华", "五星", "无所谓价格", "预算充足"],
    "低": ["便宜", "实惠", "穷游", "预算有限", "性价比", "低预算", "节约"],
    "中": ["适中", "中等", "正常消费", "合理价格"],
}

_EXCLUDED_PATTERNS = [
    r"不喜欢(.{1,10})",
    r"不想去(.{1,10})",
    r"避开(.{1,10})",
    r"不要(.{1,6}的?[地方|景点|餐厅]?)",
    r"不去(.{1,8})",
]

_PREFERRED_CATEGORIES = {
    "美食": ["美食", "吃", "餐厅", "小吃", "火锅", "烧烤", "川菜"],
    "文化": ["文化", "历史", "博物馆", "古迹", "古街", "遗址"],
    "自然": ["自然", "山", "湖", "公园", "风景", "户外", "徒步"],
    "购物": ["购物", "商场", "市场", "纪念品", "手工艺"],
    "休闲": ["休闲", "轻松", "散步", "咖啡", "茶馆", "慢节奏"],
}

# ── 深层偏好规则（R1 新增）──────────────────────────────────────────────

_DIETARY_KEYWORDS = {
    "素食": ["素食", "素菜", "全素", "蔬食", "不吃肉", "vegetarian", "吃素"],
    "纯素": ["纯素", "vegan", "全植物"],
    "清真": ["清真", "halal", "穆斯林", "不吃猪肉", "伊斯兰"],
    "无辣": ["不吃辣", "不能辣", "怕辣", "无辣"],
    "海鲜过敏": ["海鲜过敏", "对海鲜过敏", "不吃海鲜", "过敏海鲜"],
}

_NATIONALITY_KEYWORDS = {
    "韩国": ["韩国人", "我是韩国", "来自韩国", "韩国来的", "Korean"],
    "日本": ["日本人", "我是日本", "来自日本", "Japanese"],
    "穆斯林": ["穆斯林", "Muslim", "伊斯兰教徒"],
    "西方": ["外国人", "欧美", "西方", "老外"],
}

# 国籍 → 自动追加菜系偏好
_NATIONALITY_TO_CUISINE = {
    "韩国": ["韩国料理", "韩式烤肉", "韩国菜"],
    "日本": ["日本料理", "日料", "寿司"],
    "穆斯林": ["清真餐厅", "清真美食"],
}

_PACE_KEYWORDS = {
    "打卡党": ["打卡", "拍照", "网红", "ins风", "拍拍拍"],
    "深度游": ["深度", "慢慢逛", "细细体验", "不赶", "沉浸"],
    "慢节奏": ["慢节奏", "放松", "度假", "悠闲", "不着急"],
    "高效游": ["效率", "行程紧", "时间少", "快游", "一天游"],
}

_PHYSICAL_KEYWORDS = {
    "老人小孩": ["老人", "老年", "长辈", "奶奶", "爷爷", "幼儿", "婴儿", "小宝宝"],
    "户外达人": ["徒步", "爬山", "骑行", "户外", "探险", "强度大"],
}

_CHAIN_KEYWORDS = ["连锁", "品牌", "靠谱", "稳定", "保障", "知名品牌", "大牌"]
_TRENDING_KEYWORDS = ["网红", "热门", "流行", "当下", "爆款", "排队", "打卡", "种草", "小红书", "抖音推荐"]


def extract_from_messages(
    messages: list,
    existing: Optional[WorkingContext] = None,
) -> WorkingContext:
    """
    从对话消息中提取/更新工作记忆（规则提取 + 累积）

    Args:
        messages : LangGraph 消息列表
        existing : 已有的工作记忆（新提取的信息会合并进去）

    Returns:
        更新后的 WorkingContext
    """
    ctx = existing.copy() if existing else default_working_context()

    # 只处理用户消息
    from langchain_core.messages import HumanMessage
    user_texts = [
        str(m.content)
        for m in messages
        if isinstance(m, HumanMessage)
    ]
    full_text = " ".join(user_texts)

    # ── 旅行风格 ──────────────────────────────────────────────────────
    if not ctx.get("travel_style"):
        for style, keywords in _STYLE_KEYWORDS.items():
            if any(kw in full_text for kw in keywords):
                ctx["travel_style"] = style
                break

    # ── 预算档次 ──────────────────────────────────────────────────────
    if not ctx.get("budget_level"):
        for level, keywords in _BUDGET_KEYWORDS.items():
            if any(kw in full_text for kw in keywords):
                ctx["budget_level"] = level
                break

    # ── 出行人数 ──────────────────────────────────────────────────────
    if not ctx.get("party_size"):
        m = re.search(r"(\d+)\s*[个人名位口]", full_text)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 20:
                ctx["party_size"] = n

    # ── 偏好品类（累积） ──────────────────────────────────────────────
    existing_cats = set(ctx.get("preferred_categories", []))
    for category, keywords in _PREFERRED_CATEGORIES.items():
        if any(kw in full_text for kw in keywords):
            existing_cats.add(category)
    ctx["preferred_categories"] = list(existing_cats)

    # ── 排除关键词（累积） ────────────────────────────────────────────
    existing_excludes = set(ctx.get("excluded_keywords", []))
    for pattern in _EXCLUDED_PATTERNS:
        for match in re.finditer(pattern, full_text):
            keyword = match.group(1).strip("的 ，。！？")
            if keyword and len(keyword) <= 8:
                existing_excludes.add(keyword)
    ctx["excluded_keywords"] = list(existing_excludes)

    # ── 特殊需求 ──────────────────────────────────────────────────────
    needs = set(ctx.get("special_needs", []))
    if any(kw in full_text for kw in ["轮椅", "无障碍", "行动不便"]):
        needs.add("无障碍")
    if any(kw in full_text for kw in ["婴儿车", "推车", "宝宝"]):
        needs.add("婴儿车友好")
    if any(kw in full_text for kw in ["宠物", "狗", "猫", "带狗"]):
        needs.add("宠物友好")
    ctx["special_needs"] = list(needs)

    # ── 饮食限制（R1 新增）────────────────────────────────────────────
    if not ctx.get("dietary"):
        for dietary_type, keywords in _DIETARY_KEYWORDS.items():
            if any(kw in full_text for kw in keywords):
                ctx["dietary"] = dietary_type
                break

    # ── 国籍 / 文化背景（R1 新增）─────────────────────────────────────
    if not ctx.get("nationality"):
        for nat, keywords in _NATIONALITY_KEYWORDS.items():
            if any(kw in full_text for kw in keywords):
                ctx["nationality"] = nat
                # 自动追加对应菜系偏好
                extra_cuisines = _NATIONALITY_TO_CUISINE.get(nat, [])
                existing_cuisine = set(ctx.get("cuisine_pref", []))
                existing_cuisine.update(extra_cuisines)
                ctx["cuisine_pref"] = list(existing_cuisine)
                break

    # ── 菜系偏好（累积，R1 新增）──────────────────────────────────────
    cuisine_patterns = {
        "韩国料理": ["韩餐", "韩国菜", "韩式", "韩国料理", "烤肉"],
        "日本料理": ["日料", "日本料理", "寿司", "拉面", "刺身"],
        "火锅": ["火锅", "涮锅", "串串"],
        "本地特色": ["本地菜", "当地菜", "地道", "地方特色", "本地风味"],
        "川菜": ["川菜", "四川菜", "麻辣"],
        "粤菜": ["粤菜", "广东菜", "早茶", "点心"],
        "西餐": ["西餐", "牛排", "意大利", "法餐", "西式"],
        "清真餐厅": ["清真餐", "清真菜", "清真食品"],
        "素食餐厅": ["素食餐", "素菜馆", "蔬食"],
    }
    existing_cuisine = set(ctx.get("cuisine_pref", []))
    for cuisine, keywords in cuisine_patterns.items():
        if any(kw in full_text for kw in keywords):
            existing_cuisine.add(cuisine)
    ctx["cuisine_pref"] = list(existing_cuisine)

    # ── 旅行节奏（R1 新增）────────────────────────────────────────────
    if not ctx.get("pace"):
        for pace_type, keywords in _PACE_KEYWORDS.items():
            if any(kw in full_text for kw in keywords):
                ctx["pace"] = pace_type
                break

    # ── 体力水平（R1 新增）────────────────────────────────────────────
    if not ctx.get("physical_level"):
        for level, keywords in _PHYSICAL_KEYWORDS.items():
            if any(kw in full_text for kw in keywords):
                ctx["physical_level"] = level
                break

    # ── 连锁/网红偏好（R1 新增）──────────────────────────────────────
    if not ctx.get("prefer_chain") and any(kw in full_text for kw in _CHAIN_KEYWORDS):
        ctx["prefer_chain"] = True
    if not ctx.get("prefer_trending") and any(kw in full_text for kw in _TRENDING_KEYWORDS):
        ctx["prefer_trending"] = True

    # ── 明确回避（R1 新增，与 excluded_keywords 对齐）───────────────
    avoid_set = set(ctx.get("avoid", []))
    for pattern in _EXCLUDED_PATTERNS:
        for match in re.finditer(pattern, full_text):
            keyword = match.group(1).strip("的 ，。！？")
            if keyword and len(keyword) <= 8:
                avoid_set.add(keyword)
    ctx["avoid"] = list(avoid_set)

    return ctx


def format_for_prompt(ctx: Optional[WorkingContext]) -> str:
    """将工作记忆格式化为可注入 system prompt 的自然语言文本"""
    if not ctx:
        return ""

    lines = []

    if ctx.get("travel_style"):
        lines.append(f"旅行风格：{ctx['travel_style']}")
    if ctx.get("party_size"):
        lines.append(f"出行人数：{ctx['party_size']}人")
    if ctx.get("budget_level"):
        lines.append(f"预算档次：{ctx['budget_level']}等")
    if ctx.get("preferred_categories"):
        lines.append(f"偏好品类：{'、'.join(ctx['preferred_categories'])}")
    # 深层偏好
    if ctx.get("nationality"):
        lines.append(f"用户国籍/文化背景：{ctx['nationality']}（推荐搜索时注意适配）")
    if ctx.get("dietary"):
        lines.append(f"饮食限制：{ctx['dietary']}（严格遵守，推荐符合条件的餐厅）")
    if ctx.get("cuisine_pref"):
        lines.append(f"偏好菜系：{'、'.join(ctx['cuisine_pref'])}")
    if ctx.get("pace"):
        lines.append(f"旅行节奏：{ctx['pace']}")
    if ctx.get("physical_level"):
        lines.append(f"体力水平：{ctx['physical_level']}（安排活动时注意强度）")
    if ctx.get("prefer_chain"):
        lines.append("偏好连锁/品牌：是（优先推荐稳定有保障的连锁品牌）")
    if ctx.get("prefer_trending"):
        lines.append("偏好网红热门：是（优先推荐当下热门、人气旺盛的地点）")
    if ctx.get("excluded_keywords") or ctx.get("avoid"):
        all_excludes = list(set((ctx.get("excluded_keywords") or []) + (ctx.get("avoid") or [])))
        lines.append(f"排除偏好：{'、'.join(all_excludes)}")
    if ctx.get("special_needs"):
        lines.append(f"特殊需求：{'、'.join(ctx['special_needs'])}")

    if not lines:
        return ""

    return "用户本次对话偏好：\n" + "\n".join(f"  - {line}" for line in lines)
