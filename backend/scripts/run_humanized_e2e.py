"""Run a real multi-turn chat -> task parse -> itinerary acceptance journey.

The runner uses the configured local BreezeTravel service and stores no token
or API key.  It is intentionally small enough to rerun after prompt, routing,
or planner changes.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import requests

from app.constraints.location import place_is_human_suitable

TURN_1 = "我们一家三口带6岁孩子去上海玩三天，只在闵行区活动，请推荐景点、美食和酒店，步行别太累。"
TURN_2 = "酒店不要每天换，每天晚上都回同一家；再给我一些适合孩子和长辈的室内景点和吃饭地方。"
TASK_TEXT = "上海三日游，2位成人带1个孩子和1位长辈，只在闵行区活动，步行别太累，每天晚上都回同一家酒店"


def _read_sse(response: requests.Response) -> list[dict]:
    events: list[dict] = []
    for raw in response.iter_lines(decode_unicode=True):
        if raw and raw.startswith("data:"):
            events.append(json.loads(raw[5:].strip()))
    return events


def _chat(base: str, headers: dict[str, str], payload: dict) -> dict:
    with requests.post(f"{base}/chat", headers=headers, json=payload, stream=True, timeout=150) as response:
        response.raise_for_status()
        events = _read_sse(response)
    places_by_id = {
        event["data"]["place"]["place_id"]: event["data"]["place"]
        for event in events
        if event.get("event") == "place"
    }
    places = list(places_by_id.values())
    text = "".join(
        event["data"]["delta"] for event in events if event.get("event") == "text"
    )
    return {
        "places": places,
        "text": text,
        "errors": [event for event in events if event.get("event") == "error"],
        "tool_failures": [
            event["data"]["summary"]
            for event in events
            if event.get("event") == "thinking" and "暂时不可用" in event["data"]["summary"]
        ],
    }


def run(base_url: str) -> dict:
    base = base_url.rstrip("/") + "/api"
    run_id = uuid4().hex[:10]
    email = f"e2e+humanized-{run_id}@breezetravel.local"
    register_response = requests.post(
        f"{base}/auth/email-register",
        json={
            "email": email,
            "password": f"E2e-{uuid4().hex}!",
            "nickname": "E2E",
        },
        timeout=20,
    )
    register_response.raise_for_status()
    auth = register_response.json()
    user_id = auth["user_id"]
    room_id = f"eval-room-{uuid4().hex[:10]}"
    thread_id = f"eval-thread-{uuid4().hex[:10]}"
    headers = {"Authorization": f"Bearer {auth['token']}"}

    room_response = requests.post(
        f"{base}/room",
        headers=headers,
        json={
            "room_id": room_id,
            "thread_id": thread_id,
            "trip_city": "上海",
            "trip_days": 3,
            "user_id": user_id,
            "nickname": "E2E",
        },
        timeout=20,
    )
    room_response.raise_for_status()

    unique_places: dict[str, dict] = {}
    turn_results = []
    for turn_index, message in enumerate((TURN_1, TURN_2), start=1):
        result = _chat(base, headers, {
            "thread_id": thread_id,
            "room_id": room_id,
            "user_id": user_id,
            "message": message,
            "trip_city": "上海",
            "selected_place_ids": [],
        })
        for place in result["places"]:
            unique_places[place["place_id"]] = place
        districts = sorted({place.get("district") for place in result["places"] if place.get("district")})
        implausible_places = [
            place["name"] for place in result["places"]
            if not place_is_human_suitable(place)
        ]
        turn_results.append({
            "turn": turn_index,
            "message": message,
            "place_count": len(result["places"]),
            "districts": districts,
            "text": result["text"],
            "errors": result["errors"],
            "tool_failures": result["tool_failures"],
            "implausible_places": implausible_places,
            "passed": bool(result["places"])
            and districts == ["闵行区"]
            and "闵行区" in result["text"]
            and not result["errors"]
            and not result["tool_failures"]
            and not implausible_places,
        })

    parse_response = requests.post(
        f"{base}/room/{room_id}/task/parse",
        headers=headers,
        json={"text": TASK_TEXT, "default_city": "上海", "default_days": 3},
        timeout=30,
    )
    parse_response.raise_for_status()
    task_spec = parse_response.json()["task_spec"]

    optimize_response = requests.post(
        f"{base}/optimize",
        headers=headers,
        json={
            "thread_id": thread_id,
            "room_id": room_id,
            "places": list(unique_places.values()),
            "trip_days": 3,
            "task_spec": task_spec,
        },
        timeout=180,
    )
    optimize_response.raise_for_status()
    optimized = optimize_response.json()
    days = optimized["itinerary"]["days"]
    last_slots = [day["slots"][-1] if day["slots"] else None for day in days]
    hotel_ids = [slot["place_id"] if slot else None for slot in last_slots]
    itinerary_districts = sorted({
        slot["place"].get("district")
        for day in days
        for slot in day["slots"]
        if slot.get("place") and slot["place"].get("district")
    })
    hotel_checks = [
        check["reason_code"]
        for check in (optimized.get("verification_report") or {}).get("checks", [])
        if check["reason_code"].startswith("DAILY_HOTEL")
    ]
    verification_checks = (optimized.get("verification_report") or {}).get("checks", [])
    violated_checks = [
        {"constraint_id": check["constraint_id"], "reason_code": check["reason_code"]}
        for check in verification_checks
        if check["status"] == "VIOLATED"
    ]
    unknown_checks = [
        {"constraint_id": check["constraint_id"], "reason_code": check["reason_code"]}
        for check in verification_checks
        if check["status"] == "UNKNOWN"
    ]
    itinerary_result = {
        "day_count": len(days),
        "days": [
            {
                "day_index": day["day_index"],
                "slots": [
                    {
                        "start_time": slot["start_time"],
                        "end_time": slot["end_time"],
                        "place_id": slot["place_id"],
                        "name": slot["place"]["name"],
                        "category": slot["place"]["category"],
                        "district": slot["place"].get("district"),
                    }
                    for slot in day["slots"]
                ],
            }
            for day in days
        ],
        "last_categories": [slot["place"]["category"] if slot else None for slot in last_slots],
        "hotel_ids": hotel_ids,
        "itinerary_districts": itinerary_districts,
        "verification_status": (optimized.get("verification_report") or {}).get("overall_status"),
        "daily_hotel_checks": hotel_checks,
        "violated_checks": violated_checks,
        "unknown_checks": unknown_checks,
    }
    itinerary_result["humanized_checks"] = {
        "implausible_attractions": [
            slot["place"]["name"]
            for day in days
            for slot in day["slots"]
            if not place_is_human_suitable(slot["place"])
        ],
        "hotel_returns_after_21_30": [
            day["day_index"]
            for day, hotel_slot in zip(days, last_slots)
            if hotel_slot and hotel_slot["start_time"] > "21:30"
        ],
        "days_without_activity": [
            day["day_index"]
            for day in days
            if not any(slot["place"]["category"] == "attraction" for slot in day["slots"])
        ],
        "reused_food_ids": sorted({
            slot["place_id"]
            for day in days
            for slot in day["slots"]
            if slot["place"]["category"] == "food"
            and sum(
                1
                for other_day in days
                for other in other_day["slots"]
                if other["place_id"] == slot["place_id"]
            ) > 1
        }),
    }
    itinerary_result["passed"] = (
        len(days) == 3
        and itinerary_result["last_categories"] == ["hotel"] * 3
        and len(set(hotel_ids)) == 1
        and itinerary_districts == ["闵行区"]
        and hotel_checks == ["DAILY_HOTEL_ANCHORED"] * 3
        and not violated_checks
        and not itinerary_result["humanized_checks"]["implausible_attractions"]
        and not itinerary_result["humanized_checks"]["hotel_returns_after_21_30"]
        and not itinerary_result["humanized_checks"]["days_without_activity"]
    )

    service_metrics = requests.get(f"{base_url.rstrip('/')}/metrics", timeout=20).json()
    model_usage = service_metrics.get("model_usage") or {}
    observed_models = sorted({label.split(":", 1)[0] for label in model_usage})
    report = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "service_url": base_url,
        "mode": {
            "demo_mode": service_metrics.get("demo_mode"),
            "amap_mock": service_metrics.get("amap_mock"),
            "observed_models": observed_models,
            "model_usage_snapshot": model_usage,
        },
        "turns": turn_results,
        "itinerary": itinerary_result,
    }
    report["passed"] = all(turn["passed"] for turn in turn_results) and itinerary_result["passed"]
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "results" / "humanized_e2e_latest.json",
    )
    args = parser.parse_args()
    report = run(args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
