"""
BreezeTravel MCP Server（Sprint 5）

将三个核心工具暴露为标准 MCP Server，
可被 Claude Desktop / Cursor / 任意 MCP Client 直接调用。

工具列表
--------
- search_places       : 搜索高德地图 POI（景点/餐厅/住宿）
- search_travel_notes : 检索旅行游记和避坑攻略（RAG 语义检索）
- get_weather         : 查询目的地天气预报

启动方式
--------
  # 独立进程（开发调试）
  cd backend
  python -m app.mcp_server

  # 随 docker-compose 启动（production）
  docker-compose up -d mcp-server

MCP Client 接入（Claude Desktop）
-----------------------------------
  在 claude_desktop_config.json 中添加：
  {
    "mcpServers": {
      "breezetravel": {
        "url": "http://localhost:8001/mcp"
      }
    }
  }

MCP Client 接入（Cursor）
--------------------------
  在 Cursor 设置 → MCP → 添加 Server：
  名称：BreezeTravel
  URL：http://localhost:8001/mcp

设计说明
--------
MCP Server 层只做"工具适配"，不持有状态：
- search_places / search_travel_notes 复用 amap_search.py 和 rag_retrieval.py 的底层逻辑
- 返回 JSON 序列化结果，MCP Client 可直接解析
- 降级策略与主 FastAPI 服务一致（AMAP_MOCK / DEMO_MODE 环境变量生效）
"""

import json
import asyncio
from typing import Optional

# FastMCP：Anthropic 官方 MCP Server 框架（pip install mcp）
from mcp.server.fastmcp import FastMCP

# 加载项目配置（复用 .env）
from app.config import settings

mcp = FastMCP(
    name="BreezeTravel",
    instructions=(
        "BreezeTravel 旅行规划工具集。"
        "提供高德 POI 搜索、旅游游记攻略检索（RAG）和天气查询三个工具。"
        "适合规划中国城市旅行行程时使用。"
    ),
)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 1：地点搜索（高德 POI）
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def search_places(
    query: str,
    city: str,
    category: str = "",
    prefer_trending: bool = False,
    prefer_chain: bool = False,
) -> str:
    """
    搜索高德地图 POI 地点（景点、餐厅、住宿等）。

    参数
    ----
    query          : 搜索关键词，应包含类型/风格/口味等修饰词
                     例："网红火锅""素食餐厅""5A景区""精品酒店"
    city           : 目的地城市，如"成都""北京""上海"
    category       : 可选品类过滤："景点"|"美食"|"住宿"，留空不过滤
    prefer_trending: 是否优先热门网红地点（用于"流行的""网红"等诉求）
    prefer_chain   : 是否优先连锁品牌（用于"靠谱""有保障"等诉求）

    返回
    ----
    JSON 字符串，包含地点列表，每个地点含：
    - place_id    : 高德 POI ID（全局唯一标识）
    - name        : 地点名称
    - category    : 分类（attraction/food/hotel/transport）
    - address     : 详细地址
    - coords      : 经纬度 {lng, lat}
    - amap_rating : 高德评分（0-5）
    - amap_price  : 人均消费（元）
    - tags        : AI 生成标签
    - description : 一句话特点描述
    """
    try:
        # 复用 amap_search 节点的核心逻辑
        from app.agents.nodes.amap_search import run as amap_run
        from app.agents.state import default_working_context

        # 构造最小化 state
        ctx = default_working_context()
        ctx["prefer_trending"] = prefer_trending
        ctx["prefer_chain"] = prefer_chain

        state = {
            "query_rewrite": query,
            "trip_city": city,
            "working_context": ctx,
            "messages": [],
        }

        result = await amap_run(state)
        places = result.get("amap_places", [])

        # 品类过滤
        if category:
            cat_map = {"景点": "attraction", "美食": "food", "住宿": "hotel"}
            cat_val = cat_map.get(category, category.lower())
            places = [p for p in places if getattr(p, "category", None) and
                      p.category.value == cat_val]

        return json.dumps(
            [p.model_dump(exclude={"rag_meta"}) for p in places],
            ensure_ascii=False,
            indent=2,
        )

    except Exception as e:
        return json.dumps({"error": str(e), "places": []}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 2：游记攻略检索（RAG）
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def search_travel_notes(
    query: str,
    city: str,
    top_k: int = 5,
) -> str:
    """
    检索真实旅行者的游记、攻略和避坑经验（RAG 语义检索）。

    使用 HyDE + 混合检索（BM25 + pgvector）+ Cross-Encoder 重排序。

    参数
    ----
    query : 检索内容，如"成都锦里避坑攻略""带孩子去熊猫基地注意事项"
    city  : 目的地城市
    top_k : 返回的最相关段落数量（默认 5）

    返回
    ----
    JSON 字符串，包含游记段落列表，每段含：
    - content          : 游记段落文本
    - similarity       : 相关度分数（0-1）
    - retrieval_sources: 检索来源（dense/sparse/both）
    - place_ids        : 关联的高德 POI IDs
    """
    try:
        from app.tools.rag_tool import _run_rag_search

        chunks = await _run_rag_search(query=query, city=city)
        # 按相关度截取 top_k
        chunks = sorted(chunks, key=lambda c: c.get("similarity", 0), reverse=True)[:top_k]

        return json.dumps(chunks, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e), "chunks": []}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════
# Tool 3：天气查询
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_weather(
    city: str,
    days: int = 3,
) -> str:
    """
    查询目的地未来天气预报。

    参数
    ----
    city : 城市名称，如"成都""北京"
    days : 查询未来几天，范围 1-7（默认 3）

    返回
    ----
    JSON 字符串，包含每日天气信息：
    - date       : 日期
    - condition  : 天气状况（晴/多云/小雨等）
    - temp_high  : 最高温度（℃）
    - temp_low   : 最低温度（℃）
    - suggestion : 出行建议
    """
    try:
        from app.tools.weather_tool import get_weather as weather_tool
        result = await weather_tool.ainvoke({"city": city, "days": days})
        return result
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    # 加载 .env（独立进程启动时需要）
    from dotenv import load_dotenv
    from pathlib import Path

    # 尝试加载 backend/.env 或上级目录的 .env
    for env_path in [
        Path(__file__).parent.parent / ".env",
        Path(__file__).parent.parent.parent / ".env",
    ]:
        if env_path.exists():
            load_dotenv(env_path)
            break

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    print(f"[MCP] BreezeTravel MCP Server 启动中，端口 {port}")
    print(f"[MCP] 工具：search_places / search_travel_notes / get_weather")
    print(f"[MCP] Claude Desktop 接入：http://localhost:{port}/mcp")

    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
