"""Evaluate the live Router's first-turn tool calls on a frozen blind set."""

import argparse
import asyncio
import json
from pathlib import Path

from langchain_core.messages import HumanMessage

from app.agents.nodes import router


TOOL_TO_INTENT = {"search_places": "amap", "search_travel_notes": "rag", "get_weather": "weather"}


def predicted(tools: set[str]) -> str:
    if {"search_places", "search_travel_notes"} <= tools:
        return "both"
    labels = {TOOL_TO_INTENT[tool] for tool in tools if tool in TOOL_TO_INTENT}
    return next(iter(labels)) if len(labels) == 1 else "unknown"


async def evaluate(cases):
    results = []
    for case in cases:
        state = {
            "messages": [HumanMessage(content=case["query"])],
            "trip_city": case["city"],
            "react_iterations": 0,
        }
        response = await router.run(state)
        calls = response.get("messages", [{}])[0]
        tools = {item["name"] for item in getattr(calls, "tool_calls", []) or []}
        actual = predicted(tools)
        results.append({
            **case,
            "actual_intent": actual,
            "tool_calls": sorted(tools),
            "signals": response.get("routing_signals", []),
        })
    labels = ("amap", "rag", "both", "weather")
    f1s = {label: f1_counts(results, label) for label in labels}
    return {
        "samples": len(results),
        "method": "live_router_first_turn_tool_calls",
        "router_both_f1": f1s["both"],
        "router_macro_f1": sum(f1s.values()) / len(f1s),
        "f1_by_intent": f1s,
        "results": results,
    }


def f1_counts(results, label):
    tp = sum(row["intent"] == label and row["actual_intent"] == label for row in results)
    fp = sum(row["intent"] != label and row["actual_intent"] == label for row in results)
    fn = sum(row["intent"] == label and row["actual_intent"] != label for row in results)
    return 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(evaluate(json.loads(args.cases.read_text(encoding="utf-8"))))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    keys = ("samples", "router_both_f1", "router_macro_f1", "f1_by_intent")
    print(json.dumps({key: report[key] for key in keys}, ensure_ascii=False))


if __name__ == "__main__":
    main()
