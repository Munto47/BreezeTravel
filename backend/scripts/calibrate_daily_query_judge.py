"""Prepare blind human labels and score agreement with the daily-query Judge."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HIGH_RISK_TERMS = (
    "过敏", "无障碍", "轮椅", "接驳", "班车", "家庭房", "宠物",
    "清真", "乳糖", "老人", "孩子",
)
LABEL_VALUES = {"pass": True, "fail": False, True: True, False: False}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _blind_case(row: dict[str, Any]) -> dict[str, Any]:
    output = row.get("output") or {}
    return {
        "id": row["id"],
        "city": row.get("city"),
        "intent": row.get("intent"),
        "persona": row.get("persona"),
        "dimensions": row.get("dimensions") or [],
        "query": row.get("query"),
        "places": [
            {
                key: place.get(key)
                for key in (
                    "name", "category", "district", "address", "amap_rating",
                    "amap_price", "opening_hours", "tags", "description",
                    "selection_evidence_status", "confirmation_actions",
                )
            }
            for place in output.get("places") or []
        ],
        "response_text": output.get("text") or "",
        "human_label": None,
        "human_notes": "",
    }


def prepare_calibration(report: dict[str, Any], sample_size: int = 40) -> dict[str, Any]:
    """Deterministically sample across city, intent and high-risk requests."""
    rows = list(report.get("cases") or [])
    if sample_size < 30 or sample_size > 50:
        raise ValueError("sample_size must be between 30 and 50")
    if len(rows) < sample_size:
        raise ValueError("source report has fewer cases than requested sample")

    buckets: dict[tuple[str, str, bool], list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda item: str(item.get("id") or "")):
        high_risk = any(term in str(row.get("query") or "") for term in HIGH_RISK_TERMS)
        buckets[(str(row.get("city")), str(row.get("intent")), high_risk)].append(row)

    selected: list[dict[str, Any]] = []
    city_targets = {
        city: sample_size // 3 + int(index < sample_size % 3)
        for index, city in enumerate(("北京", "上海", "杭州"))
    }
    intent_targets = {
        intent: sample_size // 5 + int(index < sample_size % 5)
        for index, intent in enumerate(("attraction", "food", "hotel", "mixed", "all"))
    }
    cell_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for rows in buckets.values():
        for row in rows:
            cell_rows[(str(row.get("city")), str(row.get("intent")))].append(row)
    allocation = _balanced_allocation(cell_rows, city_targets, intent_targets)
    for cell in sorted(allocation):
        rows = sorted(cell_rows[cell], key=lambda row: (
            not any(term in str(row.get("query") or "") for term in HIGH_RISK_TERMS),
            str(row.get("id") or ""),
        ))
        selected.extend(rows[:allocation[cell]])
    selected.sort(key=lambda row: str(row.get("id") or ""))

    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "instructions": {
            "label_values": ["pass", "fail"],
            "rule": "只根据请求、候选卡片和回复文本人工判断整体是否可用；不要查看 Judge 输出。",
            "required": "每条填写 human_label；失败原因写入 human_notes。",
        },
        "sample_size": len(selected),
        "strata": {
            "by_city": dict(sorted(_count(selected, "city").items())),
            "by_intent": dict(sorted(_count(selected, "intent").items())),
            "high_risk": sum(
                any(term in str(row.get("query") or "") for term in HIGH_RISK_TERMS)
                for row in selected
            ),
        },
        "cases": [_blind_case(row) for row in selected],
    }


def _balanced_allocation(
    cell_rows: dict[tuple[str, str], list[dict[str, Any]]],
    city_targets: dict[str, int],
    intent_targets: dict[str, int],
) -> dict[tuple[str, str], int]:
    """Solve exact city/intent quotas as a small deterministic max-flow."""
    source = "source"
    sink = "sink"
    cities = tuple(city_targets)
    intents = tuple(intent_targets)
    capacity: dict[tuple[str, str], int] = {}
    adjacency: dict[str, list[str]] = defaultdict(list)

    def add_edge(left: str, right: str, value: int) -> None:
        capacity[(left, right)] = value
        capacity[(right, left)] = 0
        adjacency[left].append(right)
        adjacency[right].append(left)

    for city in cities:
        add_edge(source, f"city:{city}", city_targets[city])
        for intent in intents:
            add_edge(
                f"city:{city}", f"intent:{intent}",
                len(cell_rows.get((city, intent), [])),
            )
    for intent in intents:
        add_edge(f"intent:{intent}", sink, intent_targets[intent])

    flow: dict[tuple[str, str], int] = defaultdict(int)
    total_flow = 0
    while True:
        parent: dict[str, str | None] = {source: None}
        queue = [source]
        for node in queue:
            for neighbor in adjacency[node]:
                if neighbor in parent:
                    continue
                residual = capacity[(node, neighbor)] - flow[(node, neighbor)]
                if residual <= 0:
                    continue
                parent[neighbor] = node
                queue.append(neighbor)
                if neighbor == sink:
                    break
            if sink in parent:
                break
        if sink not in parent:
            break
        increment = 10**9
        node = sink
        while parent[node] is not None:
            previous = parent[node]
            increment = min(increment, capacity[(previous, node)] - flow[(previous, node)])
            node = previous
        node = sink
        while parent[node] is not None:
            previous = parent[node]
            flow[(previous, node)] += increment
            flow[(node, previous)] -= increment
            node = previous
        total_flow += increment

    required = sum(city_targets.values())
    if total_flow != required or required != sum(intent_targets.values()):
        raise ValueError("unable to satisfy balanced city and intent targets")
    return {
        (city, intent): flow[(f"city:{city}", f"intent:{intent}")]
        for city in cities
        for intent in intents
        if flow[(f"city:{city}", f"intent:{intent}")]
    }


def _count(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row.get(field))] += 1
    return dict(counts)


def score_agreement(
    labels: dict[str, Any],
    judge_report: dict[str, Any],
    minimum_cases: int = 30,
    threshold: float = 0.85,
) -> dict[str, Any]:
    label_rows = list(labels.get("cases") or [])
    if len(label_rows) < minimum_cases:
        raise ValueError(f"at least {minimum_cases} human labels are required")
    human: dict[str, bool] = {}
    for row in label_rows:
        raw = row.get("human_label")
        normalized = raw.strip().lower() if isinstance(raw, str) else raw
        if normalized not in LABEL_VALUES:
            raise ValueError(f"missing or invalid human_label for {row.get('id')}")
        human[str(row["id"])] = LABEL_VALUES[normalized]

    judge_rows = {str(row.get("id")): row for row in judge_report.get("cases") or []}
    missing = sorted(set(human) - set(judge_rows))
    if missing:
        raise ValueError("Judge report is missing sampled cases: " + ", ".join(missing))

    confusion = {"both_pass": 0, "both_fail": 0, "human_pass_judge_fail": 0, "human_fail_judge_pass": 0}
    disagreements: list[dict[str, Any]] = []
    for case_id, human_pass in human.items():
        judge = judge_rows[case_id].get("judge") or {}
        if judge.get("error") or judge.get("skipped") or "passed" not in judge:
            raise ValueError(f"Judge result is not valid for {case_id}")
        judge_pass = bool(judge["passed"])
        if human_pass and judge_pass:
            confusion["both_pass"] += 1
        elif not human_pass and not judge_pass:
            confusion["both_fail"] += 1
        elif human_pass:
            confusion["human_pass_judge_fail"] += 1
        else:
            confusion["human_fail_judge_pass"] += 1
        if human_pass != judge_pass:
            disagreements.append({"id": case_id, "human_passed": human_pass, "judge_passed": judge_pass})

    total = len(human)
    agreed = confusion["both_pass"] + confusion["both_fail"]
    agreement = agreed / total
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_size": total,
        "agreed": agreed,
        "agreement_rate": agreement,
        "threshold": threshold,
        "passed": agreement >= threshold,
        "confusion": confusion,
        "disagreements": disagreements,
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--report", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--sample-size", type=int, default=40)
    score = subparsers.add_parser("score")
    score.add_argument("--labels", type=Path, required=True)
    score.add_argument("--judge-report", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "prepare":
        report = json.loads(args.report.read_text(encoding="utf-8"))
        payload = prepare_calibration(report, args.sample_size)
        payload["source_report"] = str(args.report.resolve())
        payload["source_report_sha256"] = _sha256(args.report)
        _write(args.output, payload)
        return

    labels = json.loads(args.labels.read_text(encoding="utf-8"))
    judge_report = json.loads(args.judge_report.read_text(encoding="utf-8"))
    payload = score_agreement(labels, judge_report)
    payload["labels_sha256"] = _sha256(args.labels)
    payload["judge_report_sha256"] = _sha256(args.judge_report)
    _write(args.output, payload)
    raise SystemExit(0 if payload["passed"] else 1)


if __name__ == "__main__":
    main()
