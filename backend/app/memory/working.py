"""
Working Memory（工作记忆）

在单次对话中维护和更新用户的结构化偏好。
每次工具调用完成后，从最新对话消息中提取偏好更新到 WorkingContext。

设计原则
--------
- 轻量：尽量用规则提取，避免每次都调用 LLM（降低延迟）
- 累积：偏好只增不减（用户说了"不喜欢商业区"就永久记录到本次会话）
- 可读：格式化为自然语言文本注入 system prompt

工作记忆格式化示例
------------------
用户偏好（本次对话提取）：
- 旅行风格：亲子游
- 出行人数：3人（2大人1小孩）
- 预算档次：中等
- 偏好品类：美食、文化景点
- 排除偏好：太商业化的地方
- 特殊需求：儿童友好
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

    return ctx


def format_for_prompt(ctx: Optional[WorkingContext]) -> str:
    """
    将工作记忆格式化为可注入 system prompt 的自然语言文本

    Returns:
        格式化后的偏好文本；如果 ctx 为 None 或全空则返回空字符串
    """
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
    if ctx.get("excluded_keywords"):
        lines.append(f"排除偏好：{'、'.join(ctx['excluded_keywords'])}")
    if ctx.get("special_needs"):
        lines.append(f"特殊需求：{'、'.join(ctx['special_needs'])}")

    if not lines:
        return ""

    return "用户本次对话偏好：\n" + "\n".join(f"  - {line}" for line in lines)
