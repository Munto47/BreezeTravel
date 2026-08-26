"""DistanceAgent：为每个簇构建驾车时间矩阵

并发触发各簇的高德 API 拉取（无 key 时降级为直线估算）。
"""

import asyncio
import aiohttp

from app.agents.nodes.optimizer import _build_time_matrix
from app.agents.planner.state import PlannerState


async def run(state: PlannerState) -> dict:
    clusters: dict[int, list] = state["clusters"]

    async with aiohttp.ClientSession() as session:
        async def _one(cid: int, places: list):
            matrix = await _build_time_matrix(session, places)
            return cid, matrix

        results = await asyncio.gather(*[_one(cid, ps) for cid, ps in clusters.items()])

    # tuple-key 的矩阵无法被 LangSmith 序列化（JSON keys 必须是 str/数字）
    # → 在写入 state 时压平为 "a__b" 字符串 key；下游通过 lookup_matrix 取回
    time_matrices: dict[int, dict] = {}
    for cid, matrix in results:
        flat: dict[str, list] = {}
        for (a_id, b_id), val in matrix.items():
            flat[f"{a_id}__{b_id}"] = list(val) if isinstance(val, tuple) else val
        time_matrices[cid] = flat

    trace = state.get("trace", []) + [
        f"[Distance] 构建 {len(time_matrices)} 个簇的时间矩阵"
    ]
    return {"time_matrices": time_matrices, "trace": trace}
