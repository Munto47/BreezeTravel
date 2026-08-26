"""
标准化 Tool 层（Sprint 2）

将核心能力封装为 LangChain @tool，供 ReAct Agent 通过 LLM tool calling 选择调用。

工具列表
--------
- search_places      : 高德地图 POI 搜索（景点/餐厅/住宿）
- search_travel_notes: 游记攻略检索（RAG）
- get_weather        : 目的地天气查询

设计原则
--------
1. 工具函数签名清晰——LLM 能从 docstring 和参数名理解何时调用
2. 返回结构化 JSON 字符串——LLM 可读，tool_executor 也可解析
3. 错误降级——工具失败时返回友好提示而非抛出异常
"""

from app.tools.amap_tool import search_places
from app.tools.rag_tool import search_travel_notes
from app.tools.weather_tool import get_weather

# ReAct Agent bind_tools() 时使用的工具列表
ALL_TOOLS = [search_places, search_travel_notes, get_weather]

__all__ = ["search_places", "search_travel_notes", "get_weather", "ALL_TOOLS"]
