"""Small, synthetic live POI check. Never prints keys or stores raw responses."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from dotenv import dotenv_values

from app.trip_understanding.amap_place import AmapPlaceResolver

EXPECTED_NAMES = {
    "故宫": {"故宫博物院"}, "鸟巢": {"国家体育场"}, "水立方": {"国家游泳中心"},
    "东方明珠": {"东方明珠广播电视塔"}, "豫园": {"上海豫园", "豫园"},
    "西湖": {"杭州西湖风景名胜区", "西湖风景名胜区"},
    "雷峰塔": {"雷峰塔景区", "雷峰塔"}, "河坊街": {"河坊街景区", "河坊街"},
    "上海博物馆": set(),  # Do not choose a campus for an unspecified museum.
}

CASES = {
    "北京": ("天安门广场", "故宫", "景山公园", "什刹海", "后海", "鸟巢", "水立方", "南锣鼓巷"),
    "上海": ("外滩", "东方明珠", "豫园", "上海博物馆", "武康路"),
    "杭州": ("西湖", "灵隐寺", "雷峰塔", "河坊街", "南宋御街"),
}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    values = dotenv_values(root / ".local-artifacts/experience/experience.env")
    resolver = AmapPlaceResolver(api_key=values.get("AMAP_API_KEY") or "")
    totals = {"queries": 0, "matched": 0, "calls": 0, "unexpected_match": 0, "errors": 0}
    observations = []
    try:
        for city, names in CASES.items():
            for name in names:
                start = time.perf_counter()
                try:
                    result = await resolver.resolve(city=city, atomic_place_name=name, category_hint="景点")
                    receipt = result.receipt
                    matched = result.place.name if result.place else None
                    totals["matched"] += int(matched is not None)
                    totals["calls"] += int(receipt.get("external_calls", 0))
                    totals["unexpected_match"] += int(matched is not None and matched not in EXPECTED_NAMES.get(name, {name}))
                    observation = {"city": city, "query": name, "matched": matched,
                        "status": receipt.get("status"), "tier": receipt.get("selection_tier"),
                        "calls": receipt.get("external_calls"), "ms": round((time.perf_counter()-start)*1000),
                        "type_conflicts": receipt.get("provider_type_conflict_candidate_count"),
                        "type_incomplete": receipt.get("provider_type_incomplete_candidate_count")}
                    observations.append(observation)
                    print(json.dumps(observation, ensure_ascii=False), flush=True)
                except Exception as exc:
                    totals["errors"] += 1
                    print(json.dumps({"query": name, "error_category": type(exc).__name__}), flush=True)
                totals["queries"] += 1
        print(json.dumps(totals), flush=True)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({"summary": totals, "observations": observations}, ensure_ascii=False, indent=2), encoding="utf-8")
        return int(totals["errors"] > 0 or totals["unexpected_match"] > 0)
    finally:
        await resolver.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
