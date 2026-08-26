from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSuitability:
    """Conservative POI suitability derived only from Provider-returned text.

    These checks do not establish that a place is open. They only reject
    explicit service-facility/category mismatches and fail closed when the
    Provider's own name/type states that visitor access is unavailable or
    internal-only.
    """

    category_exclusion_code: str | None = None
    hard_block_codes: tuple[str, ...] = ()


_SPACE_PUNCT = re.compile(r"[\s·•_/\\]+")
_SERVICE_NAME_MARKERS = (
    "公共直饮水机",
    "直饮水机",
    "公共厕所",
    "公共卫生间",
    "公共洗手间",
    "游客服务中心",
    "游客中心",
    "售票处",
)
_SERVICE_NAME_SUFFIXES = (
    "停车场",
    "停车库",
    "停车楼",
    "停车区",
)
_SERVICE_TYPE_MARKERS = (
    "公共设施;厕所",
    "公共设施;公共厕所",
    "生活服务;公共设施;直饮水",
    "生活服务;停车场",
    "汽车服务;停车场",
    "交通设施服务;停车场",
)
_ACCESS_CONFLICT_MARKERS = (
    "不对外开放",
    "暂停开放",
    "暂不开放",
    "停止开放",
    "闭园",
    "谢绝参观",
    "禁止入内",
    "维修关闭",
)
_ACCESS_UNKNOWN_MARKERS = (
    "内部设施",
    "内部使用",
    "仅限内部",
    "员工专用",
)


def _compact(value: str | None) -> str:
    return _SPACE_PUNCT.sub("", str(value or "")).casefold()


def classify_provider_suitability(
    *,
    name: str,
    provider_raw_type: str | None,
    provider_raw_typecode: str | None,
) -> ProviderSuitability:
    """Return deterministic exclusions without inferring positive access.

    ``provider_raw_typecode`` is retained for audit/binding even though no
    brittle numeric subtype allowlist is used here. A missing raw type never
    gets fabricated; explicit negative name text can still fail closed for a
    legacy snapshot.
    """

    compact_name = _compact(name)
    compact_type = _compact(provider_raw_type)
    _ = str(provider_raw_typecode or "").strip()

    if (
        any(_compact(marker) in compact_name for marker in _SERVICE_NAME_MARKERS)
        or any(compact_name.endswith(_compact(marker)) for marker in _SERVICE_NAME_SUFFIXES)
        or any(_compact(marker) in compact_type for marker in _SERVICE_TYPE_MARKERS)
    ):
        return ProviderSuitability(category_exclusion_code="WRONG_CATEGORY_SERVICE_FACILITY")

    if any(_compact(marker) in compact_name or _compact(marker) in compact_type for marker in _ACCESS_CONFLICT_MARKERS):
        return ProviderSuitability(hard_block_codes=("ACCESS_CONFLICT",))
    if any(_compact(marker) in compact_name or _compact(marker) in compact_type for marker in _ACCESS_UNKNOWN_MARKERS):
        return ProviderSuitability(hard_block_codes=("ACCESS_STATUS_UNKNOWN",))
    return ProviderSuitability()
