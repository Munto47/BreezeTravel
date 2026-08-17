"""Closed-set Amap POI classification; unknown provider types stay UNKNOWN."""

from app.schemas.place import PlaceCategory


_TYPECODE_PREFIXES = {
    "05": PlaceCategory.FOOD,
    "10": PlaceCategory.HOTEL,
    "11": PlaceCategory.ATTRACTION,
    # 14 is the broad education/culture family and also includes schools and
    # training providers. Admit only visitor-facing cultural venue subtypes.
    "1401": PlaceCategory.ATTRACTION,  # museum
    "1402": PlaceCategory.ATTRACTION,  # exhibition hall
    "1404": PlaceCategory.ATTRACTION,  # art museum/gallery
    "1405": PlaceCategory.ATTRACTION,  # library
    "15": PlaceCategory.TRANSPORT,
}

_TYPE_LABELS = {
    "餐饮": PlaceCategory.FOOD,
    "美食": PlaceCategory.FOOD,
    "住宿": PlaceCategory.HOTEL,
    "酒店": PlaceCategory.HOTEL,
    "景区": PlaceCategory.ATTRACTION,
    "风景名胜": PlaceCategory.ATTRACTION,
    "旅游景点": PlaceCategory.ATTRACTION,
    "博物馆": PlaceCategory.ATTRACTION,
    "展览馆": PlaceCategory.ATTRACTION,
    "美术馆": PlaceCategory.ATTRACTION,
    "图书馆": PlaceCategory.ATTRACTION,
    "交通": PlaceCategory.TRANSPORT,
}

_CATEGORY_TYPECODES = {
    PlaceCategory.FOOD: ("050000",),
    PlaceCategory.HOTEL: ("100000",),
    PlaceCategory.ATTRACTION: ("110000", "140100", "140200", "140400", "140500"),
    PlaceCategory.TRANSPORT: ("150000",),
}


def classify_amap_type(typecode: str = "", type_label: str = "") -> PlaceCategory:
    compact_code = str(typecode or "").strip()
    for prefix, category in _TYPECODE_PREFIXES.items():
        if compact_code.startswith(prefix):
            return category
    for label, category in _TYPE_LABELS.items():
        if label in str(type_label or ""):
            return category
    return PlaceCategory.UNKNOWN


def typecodes_for_category(category: PlaceCategory | str) -> list[str]:
    try:
        parsed = category if isinstance(category, PlaceCategory) else PlaceCategory(str(category))
    except ValueError:
        return []
    return list(_CATEGORY_TYPECODES.get(parsed, ()))
