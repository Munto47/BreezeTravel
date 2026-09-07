"""Opt-in bounded, read-only AMap audit of official hotel records.

Does not automatically certify results or edit the registry. The report keeps
public branch facts only; API credentials and request URLs are never recorded.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import time

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


async def run(args):
    values = dotenv_values(args.config_env, interpolate=False)
    if not values.get("AMAP_API_KEY"):
        raise SystemExit("AMAP configuration missing")
    registry = json.loads((ROOT / "backend/app/trip_understanding/hotel_brand_registry_v1.json").read_text(encoding="utf-8"))
    properties = [p for p in registry["properties"] if p.get("status") == "LISTED"
        and (not args.cities or p["city"] in args.cities) and (not args.brands or p["brand"] in args.brands)]
    if len(properties) > 40:
        raise SystemExit("Audit bounded to 40 official properties per run")
    sem = asyncio.Semaphore(4)
    calls = 0
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=25) as client:
        if args.runtime:
            sys.path.insert(0, str(ROOT / "backend"))
            from app.trip_understanding.stay import AmapStayCandidateProvider, HotelBrandRegistry
            from app.trip_understanding.errors import PlaceProviderUnavailableError
            adapter = AmapStayCandidateProvider(api_key=values["AMAP_API_KEY"], client=client)
            identities = HotelBrandRegistry(registry)
            properties = [p for p in properties if p.get("provider_matches")]
            original_send = client.send

            async def count_send(*send_args, **send_kwargs):
                nonlocal calls
                calls += 1
                return await original_send(*send_args, **send_kwargs)

            client.send = count_send

            async def runtime_query(prop):
                mapping = prop["provider_matches"][0]
                lng, lat = map(float, mapping["location"].split(","))
                async with sem:
                    checked_at = datetime.now(timezone.utc).isoformat()
                    try:
                        found = await adapter.search_property(city=prop["city"], name=mapping["name"], longitude=lng, latitude=lat)
                        checked = [{"id": p.canonical_place_id, "name": p.name, "address": p.area_or_address,
                            "city": p.city, "identity": identities.identity(p, identities.match(p.name))} for p in found]
                        return {"official": prop["name"], "city": prop["city"], "source_url": prop["source_url"],
                            "checked_at": checked_at, "expected_poi_id": mapping["poi_id"], "error": None,
                            "expected_branch_verified": any(p["id"].removeprefix("amap:") == mapping["poi_id"] and p["identity"]["property_identity"] == "OFFICIAL_NAME_ADDRESS" for p in checked),
                            "candidates": checked}
                    except PlaceProviderUnavailableError as exc:
                        return {"official": prop["name"], "city": prop["city"], "checked_at": checked_at,
                            "error": exc.category, "expected_branch_verified": False, "candidates": []}
            rows = await asyncio.gather(*(runtime_query(p) for p in properties))
        else:
            rows = None
        async def query(prop):
            nonlocal calls
            async with sem:
                checked_at = datetime.now(timezone.utc).isoformat()
                calls += 1
                try:
                    response = await client.get("https://restapi.amap.com/v5/place/text", params={
                        "key": values["AMAP_API_KEY"], "keywords": prop["name"], "region": prop["city"],
                        "city_limit": "true", "types": "100000", "page_size": 20, "page_num": 1,
                    })
                    response.raise_for_status()
                    payload = response.json()
                    if payload.get("status") != "1":
                        return {"official": prop, "checked_at": checked_at, "error": "PROVIDER_REJECTED", "pois": []}
                    fields = ("id", "name", "address", "cityname", "adname", "adcode", "typecode", "type", "location")
                    return {"official": prop, "checked_at": checked_at, "error": None,
                        "pois": [{key: poi.get(key) for key in fields} for poi in payload.get("pois", []) if isinstance(poi, dict)]}
                except (httpx.HTTPError, ValueError) as exc:
                    return {"official": prop, "checked_at": checked_at, "error": type(exc).__name__, "pois": []}
        if rows is None:
            rows = await asyncio.gather(*(query(p) for p in properties))
    report = {"schema_version": "official-hotel-map-audit-v1", "provenance": "LIVE_AMAP_TEXT_SEARCH",
        "mode": "RUNTIME_ADAPTER_AND_IDENTITY" if args.runtime else "RAW_POI_AUDIT",
        "http_attempts": calls, "route_calls": 0, "model_calls": 0, "maximum_concurrency": 4,
        "money_cost": None, "elapsed_seconds": round(time.perf_counter() - started, 3), "data": rows,
        "limitations": ["Official pages checked separately; returned POIs are not automatically official matches.",
            "No prices, availability, current operations or population quality conclusion."]}
    path = Path(args.output) if args.output else ROOT / ".local-artifacts/evaluation" / (
        "stay-official-properties-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"report": str(path.relative_to(ROOT)), "http_attempts": calls,
        "errors": sum(row["error"] is not None for row in rows)}, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-env", type=Path, default=ROOT / ".local-artifacts/experience/experience.env")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cities", nargs="+")
    parser.add_argument("--brands", nargs="+")
    parser.add_argument("--runtime", action="store_true", help="Check actual candidate adapter and exact branch identities")
    asyncio.run(run(parser.parse_args()))
