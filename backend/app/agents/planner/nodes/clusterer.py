"""ClustererAgent：地点分离 + K-Means 聚类

职责：
- 将输入 places 拆为 activities（参与排线）与 hotels_pool（用于挂载）
- 对 activities 调用 _kmeans_cluster 生成 trip_days 个簇
- 计算全局质心（天气节点会用到）
"""

from app.agents.nodes.optimizer import _kmeans_cluster
from app.agents.planner.state import PlannerState
from app.schemas.place import Place, PlaceCategory


async def run(state: PlannerState) -> dict:
    places: list[Place] = state["places"]
    trip_days: int = state["trip_days"]

    hotels = [p for p in places if p.category == PlaceCategory.HOTEL]
    activities = [p for p in places if p.category != PlaceCategory.HOTEL]

    if not activities:
        raise ValueError("[ClustererAgent] 没有可排线的游玩地点（activities 为空）")

    clustered = _kmeans_cluster(activities, trip_days)
    clusters: dict[int, list[Place]] = {}
    for p in clustered:
        cid = p.cluster_id or 0
        clusters.setdefault(cid, []).append(p)

    center_lat = sum(p.coords.lat for p in activities) / len(activities)
    center_lng = sum(p.coords.lng for p in activities) / len(activities)

    trace = state.get("trace", []) + [
        f"[Clusterer] activities={len(activities)} hotels={len(hotels)} "
        f"clusters={len(clusters)}"
    ]

    return {
        "activities": activities,
        "hotels_pool": list(hotels),
        "clusters": clusters,
        "center_lat": center_lat,
        "center_lng": center_lng,
        "trace": trace,
    }
