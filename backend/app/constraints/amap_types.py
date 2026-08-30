"""Closed-set Amap POI classification; unknown provider types stay UNKNOWN."""

from dataclasses import dataclass

from app.schemas.place import PlaceCategory


_TYPECODE_PREFIXES = {
    "05": PlaceCategory.FOOD,
    "0610": PlaceCategory.ATTRACTION,  # visitor-facing tourist commercial street
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
    PlaceCategory.ATTRACTION: (
        "110000",
        "140100",
        "140200",
        "140400",
        "140500",
    ),
    PlaceCategory.TRANSPORT: ("150000",),
}


@dataclass(frozen=True, slots=True)
class AmapTypeSignals:
    typecode_category: PlaceCategory
    label_category: PlaceCategory
    category: PlaceCategory
    conflict: bool
    complete: bool


def _category_from_typecode(typecode: str) -> PlaceCategory:
    compact_code = str(typecode or "").strip()
    for prefix, category in _TYPECODE_PREFIXES.items():
        if compact_code.startswith(prefix):
            return category
    return PlaceCategory.UNKNOWN


def _category_from_label(type_label: str) -> PlaceCategory:
    categories = {
        category
        for label, category in _TYPE_LABELS.items()
        if label in str(type_label or "")
    }
    return categories.pop() if len(categories) == 1 else PlaceCategory.UNKNOWN


def classify_amap_type_signals(typecode: str = "", type_label: str = "") -> AmapTypeSignals:
    """Classify independent provider signals and expose disagreement explicitly."""

    code = _category_from_typecode(typecode)
    label = _category_from_label(type_label)
    code_known = code is not PlaceCategory.UNKNOWN
    label_known = label is not PlaceCategory.UNKNOWN
    conflict = code_known and label_known and code is not label
    if conflict:
        category = PlaceCategory.UNKNOWN
    elif code_known:
        category = code
    elif label_known:
        category = label
    else:
        category = PlaceCategory.UNKNOWN
    return AmapTypeSignals(
        typecode_category=code,
        label_category=label,
        category=category,
        conflict=conflict,
        complete=bool(str(typecode or "").strip() and str(type_label or "").strip() and code_known and label_known),
    )


def classify_amap_type(typecode: str = "", type_label: str = "") -> PlaceCategory:
    return classify_amap_type_signals(typecode, type_label).category


def typecodes_for_category(category: PlaceCategory | str) -> list[str]:
    try:
        parsed = category if isinstance(category, PlaceCategory) else PlaceCategory(str(category))
    except ValueError:
        return []
    return list(_CATEGORY_TYPECODES.get(parsed, ()))
