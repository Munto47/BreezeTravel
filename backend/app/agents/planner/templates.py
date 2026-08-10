"""鱼骨节奏模板系统（SPEC §3.2）

5 套预设模板，每套定义一天的「槽位序列」：
  - 槽位约定了 category_l2 候选集、预期时长 buffer、是否必须
  - Scheduler v2 按模板填入候选地点
"""

from dataclasses import dataclass, field
from typing import Optional

from app.schemas.preferences import GroupPreferences


@dataclass
class TemplateSlot:
    slot_id: str                           # 唯一标识，如 "morning_main"
    category_l1: str                       # 景点 / 餐饮 / 夜生活 / 住宿
    category_l2_candidates: list[str]      # 可接受的二级分类
    start_hint: int                        # 建议开始时间（分钟，从0点起算）
    duration_minutes: int                  # 基础时长
    buffer_minutes: int = 15              # 时长 buffer
    is_required: bool = True               # False = 可选，排不下可跳过
    label: str = ""                        # 人类可读标签


@dataclass
class RhythmTemplate:
    template_id: str
    name: str
    slots: list[TemplateSlot] = field(default_factory=list)


# ─── 5 套预设模板 ──────────────────────────────────────────────────────────────

T_DEEP_EXPLORE = RhythmTemplate(
    template_id="T_DEEP_EXPLORE",
    name="深度文化型",
    slots=[
        TemplateSlot("morning_main",  "景点", ["博物馆","历史遗址","景区","纪念馆"],
                     start_hint=9*60,   duration_minutes=180, buffer_minutes=30,
                     is_required=True,  label="上午重头景点"),
        TemplateSlot("lunch",         "餐饮", ["餐厅","火锅","地方菜","面馆"],
                     start_hint=12*60,  duration_minutes=75,  buffer_minutes=15,
                     is_required=True,  label="午餐"),
        TemplateSlot("afternoon_walk","景点", ["街区","古镇","艺术区","公园"],
                     start_hint=14*60,  duration_minutes=90,  buffer_minutes=20,
                     is_required=False, label="下午街区漫步"),
        TemplateSlot("coffee",        "餐饮", ["咖啡馆","茶馆","甜品"],
                     start_hint=16*60,  duration_minutes=60,  buffer_minutes=10,
                     is_required=False, label="咖啡休憩"),
        TemplateSlot("afternoon_sub", "景点", ["景区","博物馆","艺术馆","展览馆"],
                     start_hint=15*60,  duration_minutes=90,  buffer_minutes=20,
                     is_required=False, label="二级景点"),
        TemplateSlot("dinner",        "餐饮", ["餐厅","火锅","地方菜","烧烤"],
                     start_hint=18*60,  duration_minutes=90,  buffer_minutes=15,
                     is_required=True,  label="晚餐"),
    ],
)

T_NIGHTLIFE = RhythmTemplate(
    template_id="T_NIGHTLIFE",
    name="夜生活型",
    slots=[
        TemplateSlot("lunch",         "餐饮", ["餐厅","地方菜","面馆","早午餐"],
                     start_hint=12*60,  duration_minutes=75,  buffer_minutes=15,
                     is_required=True,  label="午餐"),
        TemplateSlot("afternoon_main","景点", ["景区","街区","古镇","公园"],
                     start_hint=14*60,  duration_minutes=120, buffer_minutes=20,
                     is_required=True,  label="下午景点"),
        TemplateSlot("coffee",        "餐饮", ["咖啡馆","甜品","茶馆"],
                     start_hint=16*60,  duration_minutes=60,  buffer_minutes=10,
                     is_required=False, label="咖啡"),
        TemplateSlot("dinner",        "餐饮", ["餐厅","火锅","烧烤","地方菜"],
                     start_hint=18*60,  duration_minutes=90,  buffer_minutes=15,
                     is_required=True,  label="晚餐"),
        TemplateSlot("night_view",    "景点", ["夜景","观景台","灯光秀","滨江"],
                     start_hint=20*60,  duration_minutes=60,  buffer_minutes=15,
                     is_required=False, label="夜景"),
        TemplateSlot("bar",           "夜生活", ["酒吧","酒馆","清吧","夜市"],
                     start_hint=21*60,  duration_minutes=120, buffer_minutes=30,
                     is_required=True,  label="酒吧/夜市"),
    ],
)

T_FAMILY_LIGHT = RhythmTemplate(
    template_id="T_FAMILY_LIGHT",
    name="亲子轻松型",
    slots=[
        TemplateSlot("brunch",        "餐饮", ["早午餐","餐厅","面馆","粥"],
                     start_hint=9*60,   duration_minutes=75,  buffer_minutes=15,
                     is_required=True,  label="早午餐"),
        TemplateSlot("morning_main",  "景点", ["主题乐园","动物园","科技馆","自然博物馆","公园"],
                     start_hint=10*60,  duration_minutes=120, buffer_minutes=30,
                     is_required=True,  label="亲子景点"),
        TemplateSlot("rest",          "住宿", ["酒店"],
                     start_hint=13*60,  duration_minutes=90,  buffer_minutes=0,
                     is_required=False, label="午休回酒店"),
        TemplateSlot("afternoon_park","景点", ["公园","街区","广场","湖畔"],
                     start_hint=15*60,  duration_minutes=90,  buffer_minutes=20,
                     is_required=False, label="下午公园"),
        TemplateSlot("dinner",        "餐饮", ["餐厅","地方菜","火锅","儿童友好餐厅"],
                     start_hint=17*60+30, duration_minutes=75, buffer_minutes=15,
                     is_required=True,  label="早晚餐（17:30）"),
    ],
)

T_ARRIVAL = RhythmTemplate(
    template_id="T_ARRIVAL",
    name="抵达日",
    slots=[
        TemplateSlot("checkin",       "住宿", ["酒店","民宿","客栈"],
                     start_hint=15*60,  duration_minutes=30,  buffer_minutes=0,
                     is_required=True,  label="酒店 check-in"),
        TemplateSlot("dinner",        "餐饮", ["餐厅","地方菜","火锅","面馆"],
                     start_hint=18*60,  duration_minutes=75,  buffer_minutes=15,
                     is_required=True,  label="周边晚餐"),
        TemplateSlot("short_walk",    "景点", ["街区","广场","滨江","公园"],
                     start_hint=20*60,  duration_minutes=45,  buffer_minutes=10,
                     is_required=False, label="短散步"),
    ],
)

T_DEPARTURE = RhythmTemplate(
    template_id="T_DEPARTURE",
    name="离开日",
    slots=[
        TemplateSlot("breakfast",     "餐饮", ["早餐","粥","包子","面馆"],
                     start_hint=8*60,   duration_minutes=45,  buffer_minutes=10,
                     is_required=True,  label="早餐"),
        TemplateSlot("light_spot",    "景点", ["街区","公园","广场","博物馆"],
                     start_hint=9*60,   duration_minutes=90,  buffer_minutes=20,
                     is_required=False, label="轻量景点"),
        TemplateSlot("checkout_meal", "餐饮", ["餐厅","地方菜","早午餐"],
                     start_hint=11*60,  duration_minutes=60,  buffer_minutes=10,
                     is_required=False, label="离开前用餐"),
    ],
)

_ALL_TEMPLATES: dict[str, RhythmTemplate] = {
    t.template_id: t for t in [
        T_DEEP_EXPLORE, T_NIGHTLIFE, T_FAMILY_LIGHT, T_ARRIVAL, T_DEPARTURE
    ]
}


def select_template(
    day_index: int,
    trip_days: int,
    prefs: Optional[GroupPreferences] = None,
) -> RhythmTemplate:
    """按 SPEC §3.2 模板选择逻辑：抵达日/离开日强制，中间天按偏好选"""
    if day_index == 0:
        return T_ARRIVAL
    if day_index == trip_days - 1:
        return T_DEPARTURE

    if prefs is None:
        return T_DEEP_EXPLORE

    if prefs.has_kids:
        return T_FAMILY_LIGHT
    if prefs.style == "nightlife":
        return T_NIGHTLIFE
    if prefs.style == "culture":
        return T_DEEP_EXPLORE
    if prefs.style == "food":
        return T_NIGHTLIFE   # 美食型跟夜生活模板接近（晚出、多餐）
    return T_DEEP_EXPLORE    # 默认


def get_template(template_id: str) -> RhythmTemplate:
    return _ALL_TEMPLATES[template_id]
