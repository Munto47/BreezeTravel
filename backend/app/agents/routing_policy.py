"""Small deterministic guard for mixed live-data and evidence requests.

The LLM still handles open-ended requests.  This policy only protects the
known failure mode where a user explicitly needs *both* a nearby/current POI
and source-backed rules or cautions; losing either tool makes the response
incomplete even when the language model otherwise sounds fluent.
"""

from dataclasses import dataclass


_LIVE_SIGNALS = ("附近", "周边", "哪里", "推荐", "餐厅", "酒店", "住宿", "景点", "地址", "现在")
_EVIDENCE_SIGNALS = ("攻略", "避坑", "注意", "预约", "依据", "规则", "口碑", "经验", "怎么去")
_WEATHER_SIGNALS = ("天气", "气温", "下雨", "降雨", "带伞", "晴天", "温度")


@dataclass(frozen=True)
class ToolPlan:
    intent: str
    tools: tuple[str, ...]
    signals: tuple[str, ...]


def plan_tools(query: str) -> ToolPlan | None:
    """Return a forced plan only when mixed intent is explicit."""
    live = [signal for signal in _LIVE_SIGNALS if signal in query]
    evidence = [signal for signal in _EVIDENCE_SIGNALS if signal in query]
    if live and evidence:
        return ToolPlan("both", ("search_places", "search_travel_notes"), tuple(live + evidence))
    return None


def plan_simple_tools(query: str) -> ToolPlan | None:
    """Route only unambiguous one-step reads; open-ended tasks remain ReAct."""
    weather = [signal for signal in _WEATHER_SIGNALS if signal in query]
    if weather:
        return ToolPlan("weather", ("get_weather",), tuple(weather))
    live = [signal for signal in _LIVE_SIGNALS if signal in query]
    evidence = [signal for signal in _EVIDENCE_SIGNALS if signal in query]
    if evidence and not live:
        return ToolPlan("rag", ("search_travel_notes",), tuple(evidence))
    if live and not evidence:
        return ToolPlan("amap", ("search_places",), tuple(live))
    return None
