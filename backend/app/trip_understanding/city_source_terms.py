"""Literal city-word boundaries shared by source validation and query scoping."""
from __future__ import annotations

import re


_LOCAL_FEATURE_SUFFIX = re.compile(
    r"(?:[东南西北中]路|路(?:步行街)?|街|湖(?:岸|畔|边|游船)?)"
    r"(?=$|[\s，,。；;：:、/／→+（）()*！？!?]|的|逛|漫步|散步|步行|吃饭|周边|附近|沿线)"
)


def city_word_has_local_feature_suffix(source: str, city_end: int) -> bool:
    """南京西路/昆明湖岸 are local terms, not evidence of 南京/昆明.

    The following boundary is required: 昆明湖州 and 昆明湖北 are not
    silently interpreted as lakes. This establishes no POI identity or city.
    """
    return _LOCAL_FEATURE_SUFFIX.match(source, city_end) is not None
