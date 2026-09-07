"""Three owner-authorized local live journeys, never run implicitly in CI.

These newly authored inputs are not the built-in demo. Reports contain public
travel results and timings, not cookies, resource paths, candidates or secrets.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx


CASES = [("北京", "故宫博物院", "景山公园"), ("上海", "豫园", "上海城隍庙"), ("杭州", "岳王庙", "曲院风荷")]


def run_case(base: str, city: str, first: str, second: str) -> dict:
    started = time.perf_counter()
    report = {"city": city, "evidence": "LIVE_PROVIDER_EVIDENCE", "status": "RUNNING"}
    with httpx.Client(base_url=base, timeout=45, trust_env=False) as client:
        def require(response, accepted=(200,)):
            if response.status_code not in accepted:
                raise RuntimeError(f"HTTP_{response.status_code}")
            return response

        source = f"9月12日在{city}玩一天。第一站地点待定，计划10点开始，游览两小时；11点到{second}，游览1小时。只安排这两站，不额外添加其他景点。"
        created = require(client.post("/api/v3/trip-understandings", json={"mode": "FULL", "source": {"type": "TEXT", "text": source}},
                                      headers={"Idempotency-Key": uuid4().hex}), (202,))
        path = "/api/v3/trip-understandings/" + created.json()["public_resource_id"]
        def wait(path_suffix, done, deadline=150):
            until = time.monotonic() + deadline
            while time.monotonic() < until:
                response = client.get(path + path_suffix)
                if response.status_code == 200 and done(response.json()):
                    return response
                if response.status_code not in (200, 202):
                    raise RuntimeError(f"POLL_HTTP_{response.status_code}")
                time.sleep(0.5)
            raise RuntimeError("RESULT_DEADLINE_EXCEEDED")
        def result():
            return require(client.get(path + "/result"))
        def post(suffix, body=None, key=None, etag=None):
            return client.post(path + suffix, json=body, headers={"Idempotency-Key": key or uuid4().hex,
                                "If-Match": etag or result().headers["etag"]})
        response = wait("/result", lambda data: data.get("status") != "PROCESSING")
        body = response.json()
        report["first_cards_seconds"] = round(time.perf_counter() - started, 2)
        cards = body["days"][0]["activities"]
        if len(cards) != 2 or cards[0]["start_time"] != "10:00" or cards[0]["visit_duration_minutes"] != 120:
            raise RuntimeError("SEMANTIC_TIMING_OR_COVERAGE_MISMATCH")
        if "9月12日" != body["days"][0]["label"]:
            raise RuntimeError("DATE_NOT_PRESERVED")
        report["initial_card_statuses"] = [card["status"] for card in cards]
        for index, target in enumerate((first, second)):
            current = result().json()["days"][0]["activities"][index]
            if index == 0 or current["status"] != "READY":
                found = require(post("/place-candidates", {"activity_token": current["activity_token"], "query": target})).json()
                candidates = found["candidates"]
                chosen = next((item for item in candidates if target in item["name"] or item["name"] in target), None)
                if chosen is None:
                    raise RuntimeError("NO_VERIFIED_CANDIDATE")
                require(post("/commands", {"command_type": "PLACE_CONFIRM", "activity_token": current["activity_token"],
                                           "candidate_token": chosen["candidate_token"]}))
        confirmed = result()
        # The UI checks cards immediately. A later route refresh must replace
        # this earlier unknown/stale report for the same itinerary version.
        require(post("/materialize"))
        require(post("/map-renders"), (202,))
        map_view = wait("/map-renders/latest", lambda data: data["status"] != "PREPARING").json()
        if map_view["status"] not in ("AVAILABLE", "LIMITED") or len(map_view["points"]) != 2:
            raise RuntimeError("LIVE_MAP_NOT_AVAILABLE")
        if map_view["days"][0]["label"] != "9月12日":
            raise RuntimeError("MAP_DATE_NOT_PRESERVED")
        report["map_status"] = map_view["status"]
        report["places"] = [point["name"] for point in map_view["points"]]
        report["route_minutes"] = [route[route["selected_mode"]]["duration_minutes"] for day in map_view["days"] for route in day["routes"] if route["selected_mode"]]
        require(post("/materialize"))
        checks = require(client.get(path + "/checks")).json()
        conflict = next((item for item in checks["items"] if item["title"] == "这段时间来不及"), None)
        if not conflict or not conflict["can_preview"]:
            raise RuntimeError("LIVE_TIME_CONFLICT_NOT_PREVIEWABLE")
        preview = require(post("/changes/preview", {"check_token": conflict["check_token"]})).json()
        if result().headers["etag"] != confirmed.headers["etag"]:
            raise RuntimeError("PREVIEW_MUTATED_RESULT")
        change = preview["changes"][0]
        require(post("/changes/adopt", {"change_token": preview["change_token"]}))
        applied = result()
        if applied.json()["days"][0]["activities"][1]["start_time"] != change["after"]["start_time"]:
            raise RuntimeError("APPLIED_RESULT_MISMATCH")
        if applied.json()["map"]["status"] != "NEEDS_UPDATE":
            raise RuntimeError("STALE_MAP_NOT_MARKED")
        require(post("/map-renders"), (202,))
        wait("/map-renders/latest", lambda data: data["status"] != "PREPARING")
        require(post("/materialize"))
        postchecks = require(client.get(path + "/checks")).json()
        if any(item["title"] == "这段时间来不及" for item in postchecks["items"]):
            raise RuntimeError("TIME_CONFLICT_REMAINED_AFTER_REFRESH")
        require(post("/commands", {"command_type": "UNDO"}))
        if result().json()["days"][0]["activities"][1]["start_time"] != "11:00":
            raise RuntimeError("UNDO_DID_NOT_RESTORE_CONTENT")
        report.update({"status": "PASS", "preview_start": change["before"]["start_time"],
                       "adopted_start": change["after"]["start_time"], "undo_start": "11:00",
                       "elapsed_seconds": round(time.perf_counter() - started, 2)})
        require(client.delete(path, headers={"Idempotency-Key": uuid4().hex}), (204,))
        require(client.get(path + "/result"), (410,))
        report["deletion"] = "PASS"
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8006")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--city", choices=[case[0] for case in CASES])
    args = parser.parse_args()
    report = {"captured_at": datetime.now(timezone.utc).isoformat(), "cases": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        if args.city and args.city != case[0]:
            continue
        try:
            result = run_case(args.base, *case)
        except Exception as exc:
            result = {"city": case[0], "status": "FAIL", "error_type": type(exc).__name__,
                      "category": str(exc) if isinstance(exc, RuntimeError) else "UNEXPECTED_ERROR"}
        report["cases"].append(result)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    raise SystemExit(0 if all(case["status"] == "PASS" for case in report["cases"]) else 1)


if __name__ == "__main__":
    main()
