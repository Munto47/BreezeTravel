from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


CITY_POOLS = {
    "北京": (
        "故宫博物院", "景山公园", "天坛公园", "前门大街", "北海公园",
        "恭王府", "颐和园", "圆明园", "雍和宫", "国子监",
        "钟鼓楼", "什刹海", "奥林匹克森林公园", "国家体育场", "中国科学技术馆",
        "798艺术区", "首都博物馆", "中国美术馆", "北京天文馆", "陶然亭公园",
    ),
    "上海": (
        "外滩", "豫园", "上海博物馆", "人民广场", "南京东路步行街",
        "苏州河步道", "新天地", "田子坊", "武康大楼", "徐家汇书院",
        "西岸美术馆", "上海当代艺术博物馆", "陆家嘴中心绿地", "上海科技馆", "世纪公园",
        "浦东美术馆", "杨浦滨江", "鲁迅公园", "北外滩国客中心", "静安雕塑公园",
    ),
    "杭州": (
        "断桥残雪", "白堤", "孤山", "曲院风荷", "苏堤",
        "雷峰塔", "灵隐寺", "飞来峰", "永福寺", "法喜寺",
        "梅家坞", "龙井村", "拱宸桥", "小河直街", "香积寺",
        "西溪湿地国家公园", "良渚博物院", "杭州植物园", "太子湾公园", "南宋德寿宫遗址博物馆",
    ),
}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        handle.write("\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(output_root: Path) -> dict[str, object]:
    plans = []
    plan_number = 1
    for city_code, (city, pool) in zip(("bj", "sh", "hz"), CITY_POOLS.items()):
        for index in range(10):
            stop_indices = (
                index,
                (index + 1) % 20,
                (index + 3) % 20,
                (index + 6) % 20,
                (index + 10) % 20,
            )
            stops = [
                {
                    "canonical_place_id": f"g01-fixture-{city_code}-{stop_index:02d}",
                    "name": pool[stop_index],
                }
                for stop_index in stop_indices
            ]
            edges = []
            for edge_index, (origin, destination) in enumerate(zip(stops, stops[1:]), start=1):
                walking_minutes = 12 + ((index * 7 + edge_index * 5) % 39)
                transit_minutes = 16 + ((index * 3 + edge_index * 7) % 27)
                edges.append(
                    {
                        "edge_id": f"G01-MAP-E{(plan_number - 1) * 4 + edge_index:03d}",
                        "origin": origin["name"],
                        "destination": destination["name"],
                        "walking": {
                            "duration_minutes": walking_minutes,
                            "distance_meters": walking_minutes * 74,
                            "transfer_count": 0,
                        },
                        "transit": {
                            "duration_minutes": transit_minutes,
                            "distance_meters": transit_minutes * 180,
                            "transfer_count": (index + edge_index) % 3,
                        },
                    }
                )
            plans.append(
                {
                    "plan_id": f"G01-MAP-P{plan_number:03d}",
                    "city": city,
                    "stops": stops,
                    "edges": edges,
                }
            )
            plan_number += 1

    fixture = {
        "schema_version": "g01-map-positive-fixture-v1",
        "dataset_version": "g01-map-positive-v1",
        "execution_mode": "CONTROLLED_FIXTURE",
        "authority": "NON_LIVE_SYNTHETIC_ROUTE_FACTS",
        "selection_policy": "walking-within-10-minutes-v1",
        "observed_at": "2026-08-28T00:00:00Z",
        "external_calls": 0,
        "plans": plans,
    }
    fixture_path = output_root / "fixture.json"
    _write_json(fixture_path, fixture)
    generator_sha256 = _sha256(Path(__file__))
    contract = {
        "schema_version": "g01-map-positive-dataset-contract-v1",
        "dataset_version": "g01-map-positive-v1",
        "fixture_sha256": _sha256(fixture_path),
        "generator_sha256": generator_sha256,
        "required_plan_count": 30,
        "required_city_plan_counts": {"北京": 10, "上海": 10, "杭州": 10},
        "required_edge_count": 120,
        "required_usable_coverage": 1.0,
        "external_calls": 0,
        "live_provider_claim": "NOT_RUN",
        "full_text_card_gate_claim": "NOT_RUN",
    }
    _write_json(output_root / "dataset_contract.json", contract)
    return contract


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=Path("eval_data/g01_map_positive_v1"))
    args = parser.parse_args()
    print(json.dumps(generate(args.output_root), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
