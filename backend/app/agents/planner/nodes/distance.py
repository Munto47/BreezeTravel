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

    time_matrices = {cid: matrix for cid, matrix in results}

    trace = state.get("trace", []) + [
        f"[Distance] 构建 {len(time_matrices)} 个簇的时间矩阵"
    ]
    return {"time_matrices": time_matrices, "trace": trace}
