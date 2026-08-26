"""Build explicit evidence states for user-requested place attributes.

This layer does not improve retrieval and never treats generated copy, tags, or
the POI name as proof of a volatile hotel facility or policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from app.constraints.recommendation_intent import extract_budget_ceiling
from app.schemas.place import ConstraintEvidence, EvidenceStatus, Place, PlaceCategory


@dataclass(frozen=True)
class ConfirmationConstraint:
    key: str
    label: str
    triggers: tuple[str, ...]
    detail: str
    categories: tuple[PlaceCategory, ...]


_CONFIRMATION_REGISTRY = (
    ConfirmationConstraint(
        "lodging_style", "住宿风格与建筑体验",
        ("胡同氛围", "四合院", "老上海", "建筑感", "历史建筑", "杭州气质", "精品酒店"),
        "搜索关键词只能用于召回，不能证明实际建筑、室内体验或周边氛围；预订前需查看官方图片与近期住客评价确认。",
        (PlaceCategory.HOTEL,),
    ),
    ConfirmationConstraint(
        "family_room", "家庭房",
        ("家庭房", "亲子房", "一家三口", "一家四口", "带孩子住"),
        "POI 数据不能证明具体房型、床型和可住人数，预订前需联系酒店确认。",
        (PlaceCategory.HOTEL,),
    ),
    ConfirmationConstraint(
        "accessible_room", "无障碍客房",
        ("无障碍", "轮椅", "无台阶", "电梯"),
        "POI 数据不能证明无台阶入口、电梯尺寸或无障碍客房库存，预订前需联系酒店逐项确认。",
        (PlaceCategory.HOTEL,),
    ),
    ConfirmationConstraint(
        "shuttle", "接驳或班车",
        ("接驳", "班车", "接送机"),
        "路线、班次、预约条件和费用会变化，预订前需联系酒店确认。",
        (PlaceCategory.HOTEL,),
    ),
    ConfirmationConstraint(
        "pet_policy", "宠物入住",
        ("宠物", "小狗", "带狗", "带猫"),
        "宠物种类、体型、押金和清洁费属于动态政策，预订前需联系酒店确认。",
        (PlaceCategory.HOTEL,),
    ),
    ConfirmationConstraint(
        "parking", "停车条件",
        ("停车", "自驾"),
        "POI 数据不能证明住客车位、限高和收费规则，预订前需联系酒店确认。",
        (PlaceCategory.HOTEL,),
    ),
    ConfirmationConstraint(
        "laundry", "洗衣条件",
        ("洗衣", "洗衣机"),
        "酒店名称和普通 POI 数据不能证明房内或公区洗衣配置，预订前需联系酒店确认。",
        (PlaceCategory.HOTEL,),
    ),
    ConfirmationConstraint(
        "room_space", "长住所需房型与可用空间",
        ("房间别太局促", "长住", "住一周", "住十天"),
        "POI 数据不能证明房间面积、床型和长期入住时的收纳空间；预订前需逐店确认具体房型、面积和可住人数。",
        (PlaceCategory.HOTEL,),
    ),
    ConfirmationConstraint(
        "kitchen", "厨房配置",
        ("厨房", "做饭"),
        "公寓或酒店名称不能证明具体房型允许做饭，预订前需联系酒店确认。",
        (PlaceCategory.HOTEL,),
    ),
    ConfirmationConstraint(
        "quiet_room", "安静与隔音",
        ("安静", "睡眠浅", "隔音", "别太吵", "不要正对酒吧"),
        "噪声受房间朝向、临街、酒吧和施工影响，需结合近期评价并联系酒店确认房型。",
        (PlaceCategory.HOTEL,),
    ),
    ConfirmationConstraint(
        "allergen_handling", "过敏原与交叉污染",
        ("过敏",),
        "POI 数据不能证明配料、后厨分区或交叉污染控制，到店前需联系餐厅逐项确认。",
        (PlaceCategory.FOOD,),
    ),
    ConfirmationConstraint(
        "dietary_policy", "饮食要求",
        (
            "清真", "素食", "蛋奶素", "蔬食", "清淡", "少油", "少盐", "老人",
            "不辣", "不能吃辣", "不太能吃辣", "不太辣",
        ),
        "名称和标签不能证明当前菜单、用料与制作流程满足饮食要求，到店前需联系餐厅确认。",
        (PlaceCategory.FOOD,),
    ),
    ConfirmationConstraint(
        "dairy_free", "无乳糖或植物奶",
        ("乳糖", "植物奶", "不含奶"),
        "POI 数据不能证明当前门店可替换植物奶或完全不含乳制品，到店前需联系门店确认。",
        (PlaceCategory.FOOD,),
    ),
    ConfirmationConstraint(
        "private_room_quiet", "包间与安静程度",
        ("包间", "环境别太吵", "安静"),
        "包间库存和现场噪声会随时段变化，订位前需联系餐厅确认。",
        (PlaceCategory.FOOD,),
    ),
    ConfirmationConstraint(
        "attraction_accessibility", "无障碍与低强度条件",
        ("无障碍", "轮椅", "少步行", "走不了", "腿脚", "老人", "少爬坡", "少折腾"),
        "POI 数据不能证明无台阶入口、无障碍卫生间、观光车和休息点当前可用，出发前需联系场馆确认。",
        (PlaceCategory.ATTRACTION,),
    ),
    ConfirmationConstraint(
        "night_walk_conditions", "夜间人流、照明与返程条件",
        ("夜景散步", "晚上散步", "人多一点", "回地铁方便", "地铁还能回"),
        "POI 数据不能证明到访时段的人流、照明、治安和末班车条件；出发前需查看地图路线、末班车时间和近期现场信息。",
        (PlaceCategory.ATTRACTION,),
    ),
    ConfirmationConstraint(
        "seasonal_scenery", "花期与季节景观",
        ("桂花", "花期", "秋色", "红叶", "赏花"),
        "花期、变色情况和现场景观受年份与天气影响，普通 POI 字段不能证明到访日一定处于最佳观赏期；出发前需查看近期官方或现场信息。",
        (PlaceCategory.ATTRACTION,),
    ),
    ConfirmationConstraint(
        "family_dining", "儿童就餐适配",
        ("适合孩子吃饭", "对孩子友好", "孩子友好", "亲子餐厅", "带孩子", "一家三口", "一家四口"),
        "POI 数据不能证明儿童座椅、儿童餐、口味和现场等位条件；到店前需联系餐厅逐项确认。",
        (PlaceCategory.FOOD,),
    ),
    ConfirmationConstraint(
        "solo_portion", "一人份或小份菜",
        ("一人食", "一个人", "小份", "不想点一大桌"),
        "POI 数据不能证明当前菜单提供一人份、小份菜或单人套餐；点餐前需逐店联系确认份量和最低消费。",
        (PlaceCategory.FOOD,),
    ),
    ConfirmationConstraint(
        "public_transit_access", "公共交通可达性",
        ("不自驾", "公共交通", "公交方便", "地铁方便"),
        "行政区和地点名称不能证明当前公共交通路线、换乘次数与末班车；出发前需逐点用地图路线功能核实。",
        (PlaceCategory.ATTRACTION,),
    ),
    ConfirmationConstraint(
        "ticket_affordability", "免费或低价门票",
        ("免费", "便宜", "低预算", "学生预算"),
        "普通 POI 数据没有稳定的门票与优惠字段；出发前需逐点查看场馆官网、官方小程序或售票页面核实。",
        (PlaceCategory.ATTRACTION,),
    ),
    ConfirmationConstraint(
        "local_patronage", "居民常去或本地客群",
        ("居民常去", "本地人常去", "社区居民常去"),
        "普通 POI 字段不能证明当前客群构成或居民到访频率；需结合带时间戳的近期评论或到店前向商家核实。",
        (PlaceCategory.FOOD,),
    ),
    ConfirmationConstraint(
        "hotel_transit_access", "酒店到地铁的实际步行条件",
        (
            "酒店靠地铁", "靠地铁", "地铁方便的酒店", "交通方便的酒店",
            "交通方便的酒店候选", "酒店离地铁",
        ),
        "酒店名称不能完整证明实际步行入口、坡度和末班车条件；预订前需在地图核对酒店到地铁口的步行路线。",
        (PlaceCategory.HOTEL,),
    ),
    ConfirmationConstraint(
        "hotel_cleanliness_safety", "住宿卫生、正规与安全状况",
        ("干净", "卫生", "正规", "安全"),
        "普通 POI 字段不能证明当前客房卫生、消防或经营状态；预订前需查看带日期的近期住客评价、官方预订页并联系酒店确认。",
        (PlaceCategory.HOTEL,),
    ),
)

_BUDGET_TERMS = ("预算", "每晚", "元", "块", "三百", "八百", "一千")


def _requested_open_minute(user_request: str) -> int | None:
    if "六点" in user_request and "早餐" in user_request:
        return 6 * 60
    if "七点" in user_request and "早餐" in user_request:
        return 7 * 60
    if "十点半" in user_request or "22:30" in user_request:
        return 22 * 60 + 30
    return None


def _hours_cover_minute(raw: str, target: int) -> bool | None:
    if "24小时" in raw:
        return True
    windows = re.findall(r"(\d{1,2}):(\d{2})\s*[-—至]\s*(\d{1,2}):(\d{2})", raw)
    if not windows:
        return None
    for start_h, start_m, end_h, end_m in windows:
        start = int(start_h) * 60 + int(start_m)
        end = int(end_h) * 60 + int(end_m)
        if end <= start:
            end += 24 * 60
        point = target + (24 * 60 if target < start and end > 24 * 60 else 0)
        if start <= point <= end:
            return True
    return False


def build_constraint_evidence(
    place: Place,
    user_request: str,
    district_constraint: str | None = None,
) -> list[ConstraintEvidence]:
    """Resolve only evidence that the current structured POI fields support."""

    evidence: list[ConstraintEvidence] = []
    if district_constraint:
        combined_location = f"{place.district or ''} {place.address or ''}"
        matched = district_constraint in combined_location
        evidence.append(ConstraintEvidence(
            constraint="district",
            label=f"位于{district_constraint}",
            status=EvidenceStatus.VERIFIED if matched else EvidenceStatus.UNKNOWN,
            detail=(
                f"高德 POI 的行政区或地址字段包含{district_constraint}。"
                if matched else f"高德 POI 的行政区和地址字段不能证明位于{district_constraint}。"
            ),
            source="amap_poi" if matched else "unavailable_in_poi",
            value=place.district or place.address or None,
            observed_at=place.retrieval_observed_at,
            confidence=1.0 if matched else 0.0,
        ))

    budget_ceiling = extract_budget_ceiling(user_request)
    if place.category in {PlaceCategory.FOOD, PlaceCategory.HOTEL} and (
        budget_ceiling is not None or any(term in user_request for term in _BUDGET_TERMS)
    ):
        has_price = isinstance(place.amap_price, (int, float))
        price_kind = "人均参考价格" if place.category == PlaceCategory.FOOD else "住宿参考价格"
        evidence.append(ConstraintEvidence(
            constraint="nightly_price",
            label=price_kind,
            status=EvidenceStatus.VERIFIED if has_price else EvidenceStatus.UNKNOWN,
            detail=(
                f"高德 POI 提供参考价格约 {place.amap_price:g} 元；实际价格仍随日期和房型变化。"
                if has_price else "当前 POI 数据没有可核验的参考价格。"
            ),
            source="amap_poi" if has_price else "unavailable_in_poi",
            value=place.amap_price,
            observed_at=place.retrieval_observed_at,
            confidence=0.8 if has_price else 0.0,
        ))
        ceiling = budget_ceiling
        if ceiling is not None:
            satisfies = has_price and float(place.amap_price) <= ceiling
            evidence.append(ConstraintEvidence(
                constraint="budget_ceiling",
                label=f"参考价不高于 {ceiling:g} 元",
                status=EvidenceStatus.VERIFIED if satisfies else EvidenceStatus.UNKNOWN,
                detail=(
                    f"高德 POI 参考价 {place.amap_price:g} 元不高于本次上限 {ceiling:g} 元；"
                    "实时价格仍需下单前复核。"
                    if satisfies else
                    (
                        f"高德 POI 参考价 {place.amap_price:g} 元高于本次上限 {ceiling:g} 元。"
                        if has_price else "当前 POI 数据没有可与预算上限比较的参考价。"
                    )
                ),
                source="amap_poi" if has_price else "unavailable_in_poi",
                value={
                    "observed_price": place.amap_price,
                    "ceiling": ceiling,
                    "satisfies_constraint": satisfies if has_price else None,
                },
                observed_at=place.retrieval_observed_at,
                confidence=0.8 if has_price else 0.0,
            ))

    if place.category in {PlaceCategory.FOOD, PlaceCategory.ATTRACTION} and any(
        term in user_request for term in ("营业", "几点", "六点", "七点", "十点半", "夜宵", "早餐")
    ):
        has_hours = bool(str(place.opening_hours or "").strip())
        evidence.append(ConstraintEvidence(
            constraint="opening_hours",
            label="营业时间",
            status=EvidenceStatus.VERIFIED if has_hours else EvidenceStatus.REQUIRES_CONFIRMATION,
            detail=(
                f"高德 POI 记录的营业时间为 {place.opening_hours}；临时调整仍需到店前复核。"
                if has_hours else "当前 POI 数据没有可核验的营业时间，到店前需联系场所确认。"
            ),
            source="amap_poi" if has_hours else "unavailable_in_poi",
            value=place.opening_hours,
            observed_at=place.retrieval_observed_at,
            confidence=0.7 if has_hours else 0.0,
        ))
        requested_minute = _requested_open_minute(user_request)
        if requested_minute is not None:
            covers = _hours_cover_minute(str(place.opening_hours or ""), requested_minute)
            requested_label = f"{requested_minute // 60:02d}:{requested_minute % 60:02d}"
            evidence.append(ConstraintEvidence(
                constraint="requested_open_time",
                label=f"营业记录覆盖 {requested_label}",
                status=(
                    EvidenceStatus.VERIFIED if covers is True
                    else EvidenceStatus.REQUIRES_CONFIRMATION if covers is None
                    else EvidenceStatus.UNKNOWN
                ),
                detail=(
                    f"高德 POI 营业记录 {place.opening_hours} 覆盖请求时点 {requested_label}；"
                    "临时调整仍需出发前复核。"
                    if covers is True else
                    (
                        f"高德 POI 营业记录 {place.opening_hours} 不覆盖请求时点 {requested_label}。"
                        if covers is False else "当前 POI 数据不足以验证请求时点是否营业。"
                    )
                ),
                source="amap_poi" if has_hours else "unavailable_in_poi",
                value={
                    "opening_hours": place.opening_hours,
                    "requested_time": requested_label,
                    "satisfies_constraint": covers,
                },
                observed_at=place.retrieval_observed_at,
                confidence=0.7 if covers is not None else 0.0,
            ))

    for spec in _CONFIRMATION_REGISTRY:
        if place.category in spec.categories and any(term in user_request for term in spec.triggers):
            evidence.append(ConstraintEvidence(
                constraint=spec.key,
                label=spec.label,
                status=EvidenceStatus.REQUIRES_CONFIRMATION,
                detail=spec.detail,
                source="unavailable_in_poi",
                confidence=0.0,
            ))
    return evidence


def attach_constraint_evidence(
    places: list[Place],
    user_request: str,
    district_constraint: str | None = None,
) -> list[Place]:
    return [
        place.model_copy(update={
            "constraint_evidence": build_constraint_evidence(
                place, user_request, district_constraint,
            ),
        })
        for place in places
    ]
