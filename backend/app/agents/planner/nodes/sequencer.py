"""SequencerAgent：簇内最近邻 TSP 排线

将每个簇的地点按驾车时间最优顺序排列。
"""

from app.agents.nodes.optimizer import _nearest_neighbor_tsp
from app.agents.planner.state import PlannerState


async def run(state: PlannerState) -> dict:
    clusters: dict[int, list] = state["clusters"]
    time_matrices: dict[int, dict] = state.get("time_matrices", {})

    orderings: dict[int, list] = {}
    for cid, places in clusters.items():
        matrix = time_matrices.get(cid, {})
        orderings[cid] = _nearest_neighbor_tsp(places, matrix)

    trace = state.get("trace", []) + [
        f"[Sequencer] TSP 完成 {len(orderings)} 簇"
    ]
    return {"orderings": orderings, "trace": trace}
